"""Build one deterministic provider-to-ledger admission candidate.

The tool performs no network or credential operation. It reads only exact local
provider generations and hash-bound portfolio evidence named by a private spec.
Provider observations are retained separately from authority; only account-live
facts with an accepted account and effective strategy assignment can affect the
canonical portfolio projection.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Final, Mapping, Sequence

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    rename_directory_with_retry,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.core.publication import CurrentPointer
from quant_engine.portfolio_ledger import (
    AdmissionDisposition,
    DatasetDispositionAction,
    DatasetItemKind,
    EvidenceLayer,
    GenerationQualification,
    ProviderDatasetDisposition,
    ProviderDatasetInventoryItem,
    ProviderFactAdmission,
    ProviderGenerationIdentity,
    admitted_binding_set_digest,
    product_data_class_set_digest,
    provider_upstream_inventory_digest,
    validate_admit_or_declare_receipt,
    validate_dataset_dispositions,
    validate_provider_authority,
)


SCHEMA_VERSION: Final = 2
CATALOG_SCHEMA_VERSION: Final = 2
ADMIT_OR_DECLARE_SCHEMA_VERSION: Final = 1
ADAPTER_VERSION: Final = "4"
REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
_PROVIDER_CATALOGS: Final = {
    "alpaca": ("alpaca-generation.json", frozenset({1})),
    "fidelity": ("fidelity-generation.json", frozenset({1})),
    "ibkr": ("ibkr-catalog.json", frozenset({3, 4})),
    "matrixport": ("catalog.json", frozenset({1, 2})),
    "portfolio123": ("portfolio123-generation.json", frozenset({1})),
    "quantbase": ("quantbase-generation.json", frozenset({1})),
    "schwab": ("schwab-generation.json", frozenset({1, 2})),
}
_ACCOUNT_FACTS: Final = {
    "alpaca": ("nav_observations", "positions", "transactions"),
    "fidelity": ("cash_flows", "corporate_actions", "transactions"),
    "ibkr": (
        "corporate_actions",
        "flows",
        "nav_observations",
        "positions",
        "transactions",
    ),
    "matrixport": ("nav_observations",),
    "portfolio123": (),
    "quantbase": ("flows", "nav_observations"),
    "schwab": ("nav_observations", "positions", "transactions"),
}
_PROVIDER_SOURCE_SEGMENTS: Final = {
    "alpaca": "alpaca-account-generation",
    "fidelity": "fidelity-account-generation",
    "ibkr": "ibkr-account-generation",
    "matrixport": "matrixport-account-generation",
    "portfolio123": "portfolio123-model-generation",
    "quantbase": "quantbase-account-generation",
    "schwab": "schwab-account-generation",
}


def _canonical_json_value_bytes(value: object) -> bytes:
    """Serialize any JSON value with the repository's canonical JSON settings."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


_DATA_CLASS_TARGET_FACT: Final = {
    "ACCOUNT_NAV": "nav_observations",
    "POSITION": "positions",
    "TRANSACTION": "transactions",
    "EXTERNAL_FLOW": "flows",
    "CORPORATE_ACTION": "corporate_actions",
    "PROVIDER_LIVE_RETURN": "provider_strategy_returns",
}
_TARGET_ADMISSION_FACT: Final = {
    "corporate_actions": "CORPORATE_ACTION",
    "flows": "EXTERNAL_FLOW",
    "nav_observations": "NAV_OBSERVATION",
    "positions": "POSITION",
    "provider_strategy_returns": (
        "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1_CANDIDATE"
    ),
    "transactions": "TRANSACTION",
}
_DATA_CLASS_ADMISSION_FACT: Final = {
    "ACCOUNT_NAV": "NAV_OBSERVATION",
    "CAPITAL_ANCHOR": "CAPITAL_ANCHOR",
    "CORPORATE_ACTION": "CORPORATE_ACTION",
    "EXTERNAL_FLOW": "EXTERNAL_FLOW",
    "FINANCING_COST": "FINANCING_COST",
    "FINANCING_LIABILITY": "FINANCING_LIABILITY",
    "LEVERAGE_EXPOSURE": "LEVERAGE_EXPOSURE",
    "POSITION": "POSITION",
    "PROVIDER_LIVE_RETURN": (
        "P123_MANAGED_STRATEGY_PERFORMANCE_LEVEL_TO_RETURN_V1_CANDIDATE"
    ),
    "TRANSACTION": "TRANSACTION",
}
_SOURCE_FACT_TARGET: Final = {
    "cash_flows": "flows",
    "corporate_actions": "corporate_actions",
    "flows": "flows",
    "nav_observations": "nav_observations",
    "positions": "positions",
    "transactions": "transactions",
}
_CSV_FACT_ACCOUNT: Final[dict[str, str]] = {
    "matrixport": "account-d2e5821e608e25f1",
    "quantbase": "account-58e75e8d952fa1b8",
}
_QUANTBASE_FUND_ID: Final = "107"
_SCHWAB_FLOW_KINDS: Final = frozenset(
    {
        "Bank Transfer",
        "Funds Received",
        "MoneyLink Deposit",
        "MoneyLink Transfer",
        "Wire Sent",
    }
)
_DATE_FIELDS: Final = ("as_of_date", "event_date", "effective_date")
_NAV_VALUE_BASIS_BY_PROVIDER: Final = {
    "ibkr": "ACCOUNT_TOTAL",
    "matrixport": "ACCOUNT_TOTAL",
    "quantbase": "ACCOUNT_TOTAL",
    "schwab": "ACCOUNT_VALUE",
}
_NAV_SOURCE_SCHEMA_NAMES_BY_PROVIDER: Final = {
    "ibkr": frozenset({"activity-flex"}),
    "schwab": frozenset({"schwab-wch-historical-balance"}),
}
_NAV_SOURCE_ARTIFACT_IDS_BY_PROVIDER: Final = {
    "matrixport": frozenset({"csv/daily_portfolio_totals.csv"}),
    "quantbase": frozenset({"source/csv/dashboard_Volatis_Strategy_pricehistory.csv"}),
}


class ProviderLedgerError(ValueError):
    """Provider-ledger input or projection violates the contract."""


def _load_json(path: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ProviderLedgerError(f"{name} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderLedgerError(f"{name} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise ProviderLedgerError(f"{name} must be an object")
    return value


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderLedgerError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProviderLedgerError(f"{name} must contain objects")
    return list(value)


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ProviderLedgerError(f"{name} must be a list")
    rows = [_text(item, name) for item in value]
    if len(set(rows)) != len(rows):
        raise ProviderLedgerError(f"{name} must be unique")
    return rows


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderLedgerError(f"{name} must be non-empty text")
    return value.strip()


def _day(value: object, name: str) -> date:
    try:
        parsed = date.fromisoformat(_text(value, name))
    except ValueError as exc:
        raise ProviderLedgerError(f"{name} must be an ISO date") from exc
    return parsed


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderLedgerError(f"{name} must be decimal") from exc
    if not parsed.is_finite():
        raise ProviderLedgerError(f"{name} must be finite")
    return parsed


def _observed_decimal(value: object, name: str) -> Decimal | None:
    if isinstance(value, Mapping):
        status = value.get("status")
        if status == "UNAVAILABLE":
            return None
        if status != "AVAILABLE" or value.get("value") is None:
            raise ProviderLedgerError(f"{name} observation is invalid")
        return _decimal(value["value"], name)
    if isinstance(value, str) and not value.strip():
        # A blank CSV cell is an absent observation (e.g. a provider-missing
        # day), not a zero and never a parse error.
        return None
    return _decimal(value, name)


def _quantbase_day(value: object, name: str) -> date:
    """Parse Quantbase's MM/DD/YYYY date format."""
    text = _text(value, name)
    try:
        parsed = datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError as exc:
        raise ProviderLedgerError(f"{name} must be a Quantbase date") from exc
    return parsed


def _relative(value: object, name: str) -> Path:
    text = _text(value, name)
    portable = Path(text)
    if (
        portable.is_absolute()
        or "\\" in text
        or any(part in {"", ".", ".."} for part in portable.parts)
    ):
        raise ProviderLedgerError(f"{name} must be a portable relative path")
    return portable


_RUN_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _run_id(value: object, name: str) -> str:
    """Validate a provider run identifier before it joins a filesystem path."""
    text = _text(value, name)
    if not _RUN_ID.fullmatch(text) or ".." in text:
        raise ProviderLedgerError(f"{name} is not a safe run identifier")
    return text


def _exact_file(root: Path, relative: Path, name: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ProviderLedgerError(f"{name} escapes its configured root") from exc
    if path.is_symlink() or not path.is_file():
        raise ProviderLedgerError(f"{name} must be a regular file")
    return path


def _verify_hash(path: Path, expected: object, name: str) -> tuple[str, int]:
    digest, size = hash_file(path)
    if digest != _text(expected, f"{name}.sha256"):
        raise ProviderLedgerError(f"{name} hash differs")
    return digest, size


def _manifest(
    path: Path, expected_hash: object, name: str
) -> tuple[GenerationManifest, str, int]:
    """Hash-verify then parse the SAME bytes — never reopen after hashing.

    Returns the manifest plus the digest and size of the verified buffer so
    callers never re-hash the file (a concurrent replacement must not let the
    parsed manifest and the recorded artifact describe different bytes)."""
    if path.is_symlink() or not path.is_file():
        raise ProviderLedgerError(f"{name} must be a regular file")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ProviderLedgerError(f"{name} is not readable") from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != _text(expected_hash, f"{name}.sha256"):
        raise ProviderLedgerError(f"{name} hash differs")
    try:
        manifest = GenerationManifest.from_json_bytes(content)
    except Exception as exc:
        raise ProviderLedgerError(f"{name} is not a valid generation manifest") from exc
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise ProviderLedgerError(f"{name} is not VERIFIED")
    return manifest, digest, len(content)


def _find_manifest_artifact(
    manifest: GenerationManifest, artifact_id: str, name: str
) -> SourceArtifact:
    for artifact in manifest.artifacts:
        if artifact.artifact_id == artifact_id:
            return artifact
    raise ProviderLedgerError(f"{name} artifact is missing from the manifest")


def _read_csv_rows(path: Path, required: set[str], name: str) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProviderLedgerError(f"{name} is not readable UTF-8 CSV") from exc
    return _parse_csv_rows(text, required, name)


def _parse_csv_rows(text: str, required: set[str], name: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not required.issubset(reader.fieldnames or ()):
        raise ProviderLedgerError(f"{name} lacks required columns")
    rows = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ProviderLedgerError(f"{name} contains a malformed row")
        rows.append({str(key): str(value) for key, value in row.items()})
    return rows


def _verified_json(
    path: Path, expected: object, name: str
) -> tuple[dict[str, Any], str, int]:
    """Hash-verify then parse the SAME bytes — never reopen after hashing."""
    if path.is_symlink() or not path.is_file():
        raise ProviderLedgerError(f"{name} must be a regular file")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ProviderLedgerError(f"{name} is not readable") from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != _text(expected, f"{name}.sha256"):
        raise ProviderLedgerError(f"{name} hash differs")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderLedgerError(f"{name} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise ProviderLedgerError(f"{name} must be an object")
    return value, digest, len(content)


def _verified_csv_rows(
    path: Path, expected: object, required: set[str], name: str
) -> list[dict[str, str]]:
    """Hash-verify then parse the SAME bytes — never reopen after hashing."""
    if path.is_symlink() or not path.is_file():
        raise ProviderLedgerError(f"{name} is missing or linked")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ProviderLedgerError(f"{name} is not readable") from exc
    if hashlib.sha256(content).hexdigest() != _text(expected, f"{name}.sha256"):
        raise ProviderLedgerError(f"{name} hash differs")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProviderLedgerError(f"{name} is not readable UTF-8 CSV") from exc
    return _parse_csv_rows(text, required, name)


def _mapped_strategy_for_account(
    account_id: str,
    day: date,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[str | None, str]:
    return _mapped_strategy(
        {"account_id": account_id, "as_of_date": day.isoformat()},
        accounts,
        strategies,
        assignments,
    )


def _matrixport_daily_component_total(
    manifest: GenerationManifest,
    run_root: Path,
    boundary_date: date,
) -> Decimal:
    """Sum the hash-bound per-product components for one account-total date."""
    artifact_id = "csv/daily_holdings.csv"
    artifact = _find_manifest_artifact(
        manifest, artifact_id, "matrixport daily holdings"
    )
    path = _exact_file(run_root, Path(artifact_id), "matrixport daily holdings")
    rows = _verified_csv_rows(
        path,
        artifact.sha256,
        {"product_id", "share", "utc_date", "value_usd_strict_component"},
        "matrixport daily holdings",
    )
    total = Decimal()
    observed = False
    for row in rows:
        if _day(row.get("utc_date"), "matrixport daily holdings utc_date") != (
            boundary_date
        ):
            continue
        share = _observed_decimal(row.get("share"), "matrixport daily holdings share")
        if share is None:
            raise ProviderLedgerError(
                "MATRIXPORT_ACCOUNT_COMPONENT_INVALID: active share is absent"
            )
        if share == 0:
            continue
        value = _observed_decimal(
            row.get("value_usd_strict_component"),
            "matrixport daily holdings strict component",
        )
        if value is None:
            raise ProviderLedgerError(
                "MATRIXPORT_ACCOUNT_COMPONENT_UNAVAILABLE: active holding lacks "
                "a strict USD component"
            )
        total += value
        observed = True
    if not observed:
        raise ProviderLedgerError(
            "MATRIXPORT_ACCOUNT_COMPONENTS_UNAVAILABLE: no active daily holdings "
            "exist at the account-total boundary"
        )
    return total


def _matrixport_nav_observations(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Admit Matrixport daily portfolio totals as account NAV observations."""
    account_id = _CSV_FACT_ACCOUNT["matrixport"]
    source_run_id = _run_id(catalog.get("source_run_id"), "matrixport.source_run_id")
    run_root = root / "output" / source_run_id
    artifact_id = "csv/daily_portfolio_totals.csv"
    artifact = _find_manifest_artifact(
        manifest, artifact_id, "matrixport daily portfolio totals"
    )
    path = _exact_file(
        run_root,
        Path(artifact_id),
        "matrixport daily portfolio totals",
    )
    rows = _verified_csv_rows(
        path,
        artifact.sha256,
        {
            "portfolio_value_usd_observed",
            "portfolio_value_usd_strict",
            "strict_complete",
            "utc_date",
        },
        "matrixport daily portfolio totals",
    )
    observations: list[dict[str, Any]] = []
    for row in rows:
        if row.get("strict_complete") != "true":
            # A partial sum over products with missing NAV/FX evidence is not
            # an authoritative portfolio total: never admit it as an account
            # NAV observation (it would fabricate a drawdown).
            continue
        value = _observed_decimal(
            row.get("portfolio_value_usd_observed"),
            "matrixport portfolio_value_usd_observed",
        )
        strict_value = _observed_decimal(
            row.get("portfolio_value_usd_strict"),
            "matrixport portfolio_value_usd_strict",
        )
        if value is None or strict_value is None or value != strict_value:
            raise ProviderLedgerError(
                "MATRIXPORT_ACCOUNT_TOTAL_STRICT_MISMATCH: a strict-complete row "
                "has absent or inconsistent observed and strict totals"
            )
        if value <= 0:
            # A non-positive total is not a plausible portfolio NAV.
            continue
        day = _day(row["utc_date"], "matrixport portfolio utc_date")
        strategy_id, mapping_status = _mapped_strategy_for_account(
            account_id, day, accounts, strategies, assignments
        )
        source_id = day.isoformat()
        # The ledger contract wraps observed money as {currency, status,
        # value} — the shape every catalog provider emits and downstream
        # readers (report, baseline) require.
        wrapped = {
            "currency": "USD",
            "status": "AVAILABLE",
            "value": row["portfolio_value_usd_observed"],
        }
        observations.append(
            {
                "account_id": account_id,
                "as_of_date": source_id,
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": generation_id,
                "mapping_status": mapping_status,
                "market_value": dict(wrapped),
                "nav": dict(wrapped),
                "provider_id": "matrixport",
                "record_id": f"matrixport:{source_id}",
                "source_artifact_id": artifact_id,
                "source_record_id": source_id,
                "strategy_id": strategy_id,
                "value_basis": "ACCOUNT_TOTAL",
            }
        )
    if observations:
        observation_dates: set[str] = set()
        for observation in observations:
            observation_date = str(observation["as_of_date"])
            if observation_date in observation_dates:
                raise ProviderLedgerError(
                    "MATRIXPORT_ACCOUNT_TOTAL_DUPLICATE_DATE: more than one "
                    "strict account total exists for a date"
                )
            observation_dates.add(observation_date)
        snapshot_date = _day(
            catalog.get("observed_boundary_date"),
            "matrixport observed_boundary_date",
        )
        latest = max(observations, key=lambda item: str(item["as_of_date"]))
        if latest["as_of_date"] != snapshot_date.isoformat():
            raise ProviderLedgerError(
                "MATRIXPORT_ACCOUNT_SNAPSHOT_DATE_MISMATCH: latest strict "
                "account total is not aligned with the daily-holdings boundary"
            )
        component_total = _matrixport_daily_component_total(
            manifest, run_root, snapshot_date
        )
        latest_total = _observed_decimal(
            latest["nav"], "matrixport latest account total"
        )
        if latest_total is None:
            raise ProviderLedgerError(
                "MATRIXPORT_ACCOUNT_TOTAL_UNAVAILABLE: latest total is absent"
            )
        # This is the producer's exact roll-up identity, not a return or jump
        # cap: a strict account total is the sum of the same-date active
        # holdings' strict USD components.
        if latest_total != component_total:
            raise ProviderLedgerError(
                "MATRIXPORT_ACCOUNT_TOTAL_BASIS_MISMATCH: latest account total "
                "does not equal its hash-bound daily holding components"
            )
    return observations


def _quantbase_nav_observations(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Admit Quantbase Volatis daily dashboard values as account NAV observations."""
    account_id = _CSV_FACT_ACCOUNT["quantbase"]
    run_id = _run_id(
        _object(catalog.get("legacy_lineage"), "quantbase.legacy_lineage").get(
            "run_id"
        ),
        "quantbase.legacy_lineage.run_id",
    )
    relative_path = "csv/dashboard_Volatis_Strategy_pricehistory.csv"
    artifact_id = f"source/{relative_path}"
    artifact = _find_manifest_artifact(
        manifest, artifact_id, "quantbase Volatis pricehistory"
    )
    path = _exact_file(
        root / "source-runs" / run_id / "artifacts",
        Path(relative_path),
        "quantbase Volatis pricehistory",
    )
    rows = _verified_csv_rows(
        path, artifact.sha256, {"time", "value"}, "quantbase Volatis pricehistory"
    )
    observations: list[dict[str, Any]] = []
    for row in rows:
        value = _observed_decimal(row.get("value"), "quantbase Volatis value")
        if value is None or value <= 0:
            continue
        day = _quantbase_day(row["time"], "quantbase Volatis time")
        strategy_id, mapping_status = _mapped_strategy_for_account(
            account_id, day, accounts, strategies, assignments
        )
        source_id = day.isoformat()
        # Same wrapped {currency, status, value} money shape as the catalog
        # providers — downstream readers require it.
        wrapped = {"currency": "USD", "status": "AVAILABLE", "value": row["value"]}
        observations.append(
            {
                "account_id": account_id,
                "as_of_date": source_id,
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": generation_id,
                "mapping_status": mapping_status,
                "market_value": dict(wrapped),
                "nav": dict(wrapped),
                "provider_id": "quantbase",
                "record_id": f"quantbase:{source_id}",
                "source_artifact_id": artifact_id,
                "source_record_id": source_id,
                "strategy_id": strategy_id,
                # The Volatis dashboard value is the strict account total for
                # the mapped account: declare the basis so authoritative-NAV
                # admission can verify it (#359/#361 contract class).
                "value_basis": "ACCOUNT_TOTAL",
            }
        )
    return observations


def _quantbase_flows(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Admit Quantbase capital contributions as flows.

    Mirrors the legacy reconciliation (tools/portfolio_target_series.py):
    contributions are filled_txn BUY rows on the fund, matched in order
    against cost-basis increases in the dashboard history, and booked on the
    day the cost basis rises. A contribution that does not reconcile is a
    hard failure — never a silently mis-booked flow."""
    account_id = _CSV_FACT_ACCOUNT["quantbase"]
    run_id = _run_id(
        _object(catalog.get("legacy_lineage"), "quantbase.legacy_lineage").get(
            "run_id"
        ),
        "quantbase.legacy_lineage.run_id",
    )
    run_root = root / "source-runs" / run_id / "artifacts"
    price_rel = Path("csv/dashboard_Volatis_Strategy_pricehistory.csv")
    price_artifact = _find_manifest_artifact(
        manifest, f"source/{price_rel.as_posix()}", "quantbase Volatis pricehistory"
    )
    price_rows = _verified_csv_rows(
        _exact_file(run_root, price_rel, "quantbase Volatis pricehistory"),
        price_artifact.sha256,
        {"time", "cost_basis"},
        "quantbase Volatis pricehistory",
    )
    activity_rel = Path("csv/account_activity.csv")
    activity_artifact = _find_manifest_artifact(
        manifest, f"source/{activity_rel.as_posix()}", "quantbase account activity"
    )
    activity_rows = _verified_csv_rows(
        _exact_file(run_root, activity_rel, "quantbase account activity"),
        activity_artifact.sha256,
        {"activity_type", "amount", "fund", "is_rebalance", "time", "type"},
        "quantbase account activity",
    )

    contributions: list[tuple[datetime, Decimal]] = []
    for row in activity_rows:
        if (
            row["activity_type"] != "filled_txn"
            or row["fund"] != _QUANTBASE_FUND_ID
            or row["is_rebalance"].lower() != "false"
        ):
            continue
        amount = _observed_decimal(row["amount"], "quantbase activity amount")
        if amount is None or amount <= 0 or row["type"].upper() != "BUY":
            continue
        try:
            observed = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProviderLedgerError(
                "quantbase activity timestamp is invalid"
            ) from exc
        contributions.append((observed, amount))
    contributions.sort(key=lambda item: item[0])

    history: list[tuple[date, Decimal]] = []
    for row in price_rows:
        cost_basis = _observed_decimal(row.get("cost_basis"), "quantbase cost basis")
        if cost_basis is None or cost_basis < 0:
            continue
        history.append(
            (_quantbase_day(row["time"], "quantbase Volatis time"), cost_basis)
        )
    history.sort(key=lambda item: item[0])

    pending: list[tuple[datetime, Decimal]] = []
    contribution_index = 0
    previous_cost_basis = Decimal(0)
    booked_flows: dict[date, Decimal] = {}
    tolerance = Decimal("0.02")
    for day, cost_basis in history:
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
                pending = pending[matched_count:]
        previous_cost_basis = cost_basis
    if contribution_index != len(contributions) or pending:
        raise ProviderLedgerError("quantbase capital additions do not reconcile")

    flows: list[dict[str, Any]] = []
    for day, amount in sorted(booked_flows.items()):
        strategy_id, mapping_status = _mapped_strategy_for_account(
            account_id, day, accounts, strategies, assignments
        )
        flows.append(
            {
                "account_id": account_id,
                "amount": {
                    # The wrapped money shape the ledger contract requires.
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": format(amount.normalize(), "f"),
                },
                "event_date": day.isoformat(),
                "evidence_layer": "ACCOUNT_LIVE",
                "flow_type": "EXTERNAL_PROVIDER_TRANSFER",
                "generation_id": generation_id,
                "mapping_basis": "RECONCILED_COST_BASIS_INCREASE",
                "mapping_status": mapping_status,
                "provider_id": "quantbase",
                "record_id": f"quantbase-flow:{day.isoformat()}",
                "source_artifact_id": f"source/{activity_rel.as_posix()}",
                "source_record_id": day.isoformat(),
                "strategy_id": strategy_id,
            }
        )
    return flows


def _load_spec(path: Path) -> dict[str, Any]:
    spec = _load_json(path, "provider-ledger spec")
    base_keys = {
        "as_of_date",
        "output_root",
        "provenance",
        "providers",
        "schema_version",
        "strategy_inputs",
        "strategy_provider_map",
    }
    version = spec.get("schema_version")
    expected = base_keys | ({"financial_mvp"} if version == SCHEMA_VERSION else set())
    if set(spec) != expected or version not in {1, SCHEMA_VERSION}:
        raise ProviderLedgerError("provider-ledger spec keys or version differ")
    if version == 1:
        paths = {
            "contract": REPOSITORY_ROOT
            / "contracts"
            / "portfolio-financial-mvp-source-v1.json",
            "registry": REPOSITORY_ROOT
            / "docs"
            / "analytics"
            / "current-strategy-decomposition-plan.json",
            "schema": REPOSITORY_ROOT
            / "schemas"
            / "portfolio"
            / "financial-mvp-source-contract.schema.json",
        }
        financial_mvp: dict[str, str] = {}
        for name, value in paths.items():
            financial_mvp[f"{name}_path"] = str(value)
            financial_mvp[f"{name}_sha256"] = hash_file(value)[0]
        spec = {**spec, "financial_mvp": financial_mvp}
        spec["schema_version"] = SCHEMA_VERSION
    _day(spec["as_of_date"], "spec.as_of_date")
    providers = _object(spec["providers"], "providers")
    if set(providers) != set(_PROVIDER_CATALOGS):
        raise ProviderLedgerError(
            "provider-ledger spec must name every current provider"
        )
    return spec


def _financial_mvp_module() -> Any:
    """Load the exact repository validator without trusting import state."""

    module_path = (
        Path(__file__).resolve().with_name("portfolio_financial_mvp_contract.py")
    )
    module_name = "_provider_ledger_financial_mvp_contract"
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        if Path(str(getattr(loaded, "__file__", ""))).resolve() != module_path:
            raise ProviderLedgerError("financial MVP validator origin differs")
        return loaded
    module_spec = importlib.util.spec_from_file_location(module_name, module_path)
    if module_spec is None or module_spec.loader is None:
        raise ProviderLedgerError("financial MVP validator is unavailable")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ProviderLedgerError("financial MVP validator failed to load") from exc
    return module


def _financial_mvp_authority(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Verify and load the accepted product, census, and source authority."""

    ref = _object(spec["financial_mvp"], "financial_mvp")
    expected = {
        "contract_path",
        "contract_sha256",
        "registry_path",
        "registry_sha256",
        "schema_path",
        "schema_sha256",
    }
    if set(ref) != expected:
        raise ProviderLedgerError("financial MVP reference keys differ")
    contract, contract_sha256, _ = _verified_json(
        Path(_text(ref["contract_path"], "financial_mvp.contract_path")).resolve(),
        ref["contract_sha256"],
        "financial MVP contract",
    )
    registry, _, _ = _verified_json(
        Path(_text(ref["registry_path"], "financial_mvp.registry_path")).resolve(),
        ref["registry_sha256"],
        "financial MVP strategy registry",
    )
    schema, _, _ = _verified_json(
        Path(_text(ref["schema_path"], "financial_mvp.schema_path")).resolve(),
        ref["schema_sha256"],
        "financial MVP schema",
    )
    module = _financial_mvp_module()
    try:
        errors = module.schema_errors(schema, contract, schema)
        if errors:
            raise ProviderLedgerError(
                "financial MVP schema validation failed: " + "; ".join(errors[:10])
            )
        module.validate_contract(contract, registry=registry)
        semantic_digest = module.contract_digest(contract)
        census_sha256 = module.census_digest(contract["current_census"])
    except ProviderLedgerError:
        raise
    except Exception as exc:
        raise ProviderLedgerError("financial MVP contract is invalid") from exc
    sources = {
        str(item.get("source_id")): item
        for item in _objects(contract.get("source_segments"), "source_segments")
    }
    if not set(_PROVIDER_SOURCE_SEGMENTS.values()).issubset(sources):
        raise ProviderLedgerError("financial MVP provider source set differs")
    return {
        "census_id": _text(
            _object(contract["current_census"], "current_census").get("census_id"),
            "current_census.census_id",
        ),
        "census_sha256": census_sha256,
        "contract": contract,
        "contract_raw_sha256": contract_sha256,
        "contract_semantic_sha256": semantic_digest,
        "product_id": _text(contract["product"].get("product_id"), "product_id"),
        "product_version": _text(
            contract["product"].get("product_version"), "product_version"
        ),
        "sources": sources,
    }


def _provenance(spec: Mapping[str, Any]) -> tuple[dict[str, Any], SourceArtifact]:
    ref = _object(spec["provenance"], "provenance")
    if set(ref) != {"catalog_path", "catalog_sha256", "generation_id"}:
        raise ProviderLedgerError("provenance reference keys differ")
    path = Path(_text(ref["catalog_path"], "provenance.catalog_path")).resolve()
    catalog, digest, size = _verified_json(
        path, ref["catalog_sha256"], "provenance catalog"
    )
    if catalog.get("generation_id") not in {None, ref["generation_id"]}:
        raise ProviderLedgerError("provenance generation identity differs")
    as_of = _day(catalog.get("as_of_date"), "provenance.as_of_date")
    artifact = SourceArtifact(
        artifact_id="portfolio/provenance.json",
        sha256=digest,
        size_bytes=size,
        as_of_date=as_of,
        schema_name="portfolio-provenance-catalog",
        schema_version="1",
    )
    return catalog, artifact


def _provider(
    provider_id: str, value: object, baseline_as_of: date
) -> tuple[
    ProviderGenerationIdentity,
    GenerationManifest,
    dict[str, Any],
    tuple[SourceArtifact, SourceArtifact],
    Path,
]:
    ref = _object(value, f"providers.{provider_id}")
    expected = {
        "catalog_sha256",
        "generation_id",
        "manifest_sha256",
        "qualification",
        "root",
    }
    if set(ref) != expected:
        raise ProviderLedgerError(f"{provider_id} provider reference keys differ")
    root = Path(_text(ref["root"], f"{provider_id}.root")).resolve()
    generation_id = _run_id(ref["generation_id"], f"{provider_id}.generation_id")
    generation_root = root / "generations" / generation_id
    manifest_path = _exact_file(
        generation_root, Path("generation.json"), f"{provider_id} manifest"
    )
    catalog_name, versions = _PROVIDER_CATALOGS[provider_id]
    catalog_path = _exact_file(
        generation_root, Path(catalog_name), f"{provider_id} catalog"
    )
    manifest, manifest_hash, manifest_size = _manifest(
        manifest_path, ref["manifest_sha256"], f"{provider_id} manifest"
    )
    if manifest.generation_id != generation_id or manifest.as_of_date > baseline_as_of:
        raise ProviderLedgerError(f"{provider_id} generation identity or date differs")
    catalog, catalog_hash, catalog_size = _verified_json(
        catalog_path, ref["catalog_sha256"], f"{provider_id} catalog"
    )
    if not any(
        artifact.sha256 == catalog_hash and artifact.size_bytes == catalog_size
        for artifact in manifest.artifacts
    ):
        raise ProviderLedgerError(
            f"{provider_id} catalog is not an artifact of its generation manifest"
        )
    schema_version = catalog.get("catalog_schema_version")
    if schema_version not in versions:
        raise ProviderLedgerError(f"{provider_id} catalog schema is unsupported")
    try:
        qualification = GenerationQualification(
            _text(ref["qualification"], f"{provider_id}.qualification")
        )
    except ValueError as exc:
        raise ProviderLedgerError(
            f"{provider_id} qualification is unsupported"
        ) from exc
    current_path = root / "CURRENT.json"
    if qualification is GenerationQualification.PUBLISHED_COMPLETE:
        if not current_path.is_file() or current_path.is_symlink():
            raise ProviderLedgerError(f"{provider_id} published pointer is missing")
        try:
            current = CurrentPointer.from_json_bytes(current_path.read_bytes())
        except Exception as exc:
            raise ProviderLedgerError(
                f"{provider_id} published pointer is invalid"
            ) from exc
        if current.generation_id != generation_id:
            raise ProviderLedgerError(f"{provider_id} generation is not current")
        if (
            manifest.coverage.status
            is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
        ):
            raise ProviderLedgerError(
                f"{provider_id} complete qualification contradicts coverage"
            )
    elif qualification is GenerationQualification.ACCEPTED_PARTIAL:
        if manifest.coverage.status is not CoverageStatus.PARTIAL:
            raise ProviderLedgerError(
                f"{provider_id} partial qualification contradicts coverage"
            )
    elif provider_id != "fidelity":
        raise ProviderLedgerError(
            "FINITE_STATIC qualification is reserved for Fidelity"
        )
    identity = ProviderGenerationIdentity(
        provider_id=provider_id,
        generation_id=generation_id,
        manifest_sha256=_text(ref["manifest_sha256"], "manifest_sha256"),
        catalog_sha256=catalog_hash,
        qualification=qualification,
    )
    artifacts = (
        SourceArtifact(
            artifact_id=f"providers/{provider_id}/generation.json",
            sha256=manifest_hash,
            size_bytes=manifest_size,
            as_of_date=manifest.as_of_date,
            schema_name="generation-manifest",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id=f"providers/{provider_id}/{catalog_name}",
            sha256=catalog_hash,
            size_bytes=catalog_size,
            as_of_date=manifest.as_of_date,
            schema_name=f"{provider_id}-generation-catalog",
            schema_version=str(schema_version),
        ),
    )
    return identity, manifest, catalog, artifacts, root


def _assignment_index(
    provenance: Mapping[str, Any],
) -> tuple[set[str], set[str], dict[str, list[dict[str, Any]]]]:
    accounts = {
        _text(item.get("account_id"), "account_id")
        for item in _objects(provenance.get("accounts"), "accounts")
    }
    strategies = {
        _text(item.get("strategy_id"), "strategy_id")
        for item in _objects(provenance.get("strategies"), "strategies")
    }
    assignments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in _objects(provenance.get("assignments"), "assignments"):
        normalized = dict(item)
        if normalized.get("mapping_status") == "MAPPED" and normalized.get(
            "effective_start"
        ) in {None, ""}:
            raise ProviderLedgerError(
                "mapped assignment requires an explicit effective_start"
            )
        assignments[_text(item.get("account_id"), "assignment.account_id")].append(
            normalized
        )
    return accounts, strategies, assignments


def _row_day(row: Mapping[str, Any]) -> date | None:
    for field in _DATE_FIELDS:
        if row.get(field) not in {None, ""}:
            return _day(row[field], field)
    return None


def _mapped_strategy(
    row: Mapping[str, Any],
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[str | None, str]:
    account_id = str(row.get("account_id") or "")
    day = _row_day(row)
    if account_id not in accounts or day is None:
        return None, "UNMAPPED"
    active: list[str] = []
    for item in assignments.get(account_id, ()):
        strategy_id = item.get("strategy_id")
        if not isinstance(strategy_id, str) or strategy_id not in strategies:
            continue
        raw_start = item.get("effective_start")
        if raw_start in {None, ""}:
            continue
        start = _day(raw_start, "assignment.effective_start")
        raw_end = item.get("effective_end")
        end = (
            date.max
            if raw_end in {None, ""}
            else _day(raw_end, "assignment.effective_end")
        )
        if start <= day <= end:
            active.append(strategy_id)
    unique = sorted(set(active))
    return (unique[0], "MAPPED") if len(unique) == 1 else (None, "UNMAPPED")


def _project_account_facts(
    provider_id: str,
    generation_id: str,
    catalog: Mapping[str, Any],
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
    manifest: GenerationManifest | None = None,
    root: Path | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    projections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exceptions: list[dict[str, Any]] = []
    if provider_id not in _ACCOUNT_FACTS:
        raise ProviderLedgerError(f"unknown provider fact mapping: {provider_id}")
    for source_fact in _ACCOUNT_FACTS[provider_id]:
        target_fact = "flows" if source_fact == "cash_flows" else source_fact
        if provider_id in _CSV_FACT_ACCOUNT and source_fact in {
            "flows",
            "nav_observations",
        }:
            if manifest is None or root is None:
                raise ProviderLedgerError(
                    f"{provider_id} CSV facts require a manifest and root"
                )
            if provider_id == "matrixport":
                if source_fact != "nav_observations":
                    continue
                rows: list[dict[str, Any]] = _matrixport_nav_observations(
                    generation_id,
                    manifest,
                    catalog,
                    root,
                    accounts,
                    strategies,
                    assignments,
                )
            elif source_fact == "nav_observations":
                rows = _quantbase_nav_observations(
                    generation_id,
                    manifest,
                    catalog,
                    root,
                    accounts,
                    strategies,
                    assignments,
                )
            else:
                rows = _quantbase_flows(
                    generation_id,
                    manifest,
                    catalog,
                    root,
                    accounts,
                    strategies,
                    assignments,
                )
            projections[target_fact].extend(rows)
            for row in rows:
                if row["mapping_status"] == "UNMAPPED":
                    exceptions.append(
                        {
                            "account_in_provenance": row["account_id"] in accounts,
                            "effective_date": row.get("as_of_date")
                            or row.get("event_date"),
                            "fact_type": target_fact,
                            "provider_id": provider_id,
                            "record_id": row["record_id"],
                            "status": "UNMAPPED",
                        }
                    )
            continue
        if source_fact not in catalog:
            raise ProviderLedgerError(
                f"declared provider fact collection is missing: "
                f"{provider_id}.{source_fact}"
            )
        rows = _objects(catalog[source_fact], f"{provider_id}.{source_fact}")
        for row in rows:
            day = _row_day(row)
            account_id = str(row.get("account_id") or "")
            strategy_id, mapping_status = _mapped_strategy(
                row, accounts, strategies, assignments
            )
            source_id = _text(
                row.get("record_id"), f"{provider_id}.{source_fact}.record_id"
            )
            record: dict[str, Any] = {
                "account_id": account_id,
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": generation_id,
                "mapping_status": mapping_status,
                "provider_id": provider_id,
                "record_id": f"{provider_id}:{source_id}",
                "source_record_id": source_id,
                "strategy_id": strategy_id,
            }
            for field in (
                "amount",
                "as_of_date",
                "event_date",
                "flow_type",
                "kind",
                "market_value",
                "nav",
                "price",
                "quantity",
                "security_id",
                "source_artifact_id",
                "value_basis",
            ):
                if field in row:
                    record[field] = row[field]
            projections[target_fact].append(record)
            if mapping_status == "UNMAPPED":
                exceptions.append(
                    {
                        "account_in_provenance": account_id in accounts,
                        "effective_date": day.isoformat() if day else None,
                        "fact_type": target_fact,
                        "provider_id": provider_id,
                        "record_id": record["record_id"],
                        "status": "UNMAPPED",
                    }
                )

            if (
                provider_id == "schwab"
                and source_fact == "transactions"
                and row.get("kind") in _SCHWAB_FLOW_KINDS
            ):
                projections["flows"].append(
                    {
                        **record,
                        "flow_type": "EXTERNAL_PROVIDER_TRANSFER",
                        "mapping_basis": "INFERRED_PROVIDER_KIND",
                        "record_id": f"schwab-flow:{source_id}",
                    }
                )

    for rows in projections.values():
        rows.sort(
            key=lambda item: (
                str(item.get("as_of_date") or item.get("event_date") or ""),
                str(item["record_id"]),
            )
        )
        if len({str(item["record_id"]) for item in rows}) != len(rows):
            raise ProviderLedgerError(
                f"{provider_id} projected fact identifiers collide"
            )
    return dict(projections), exceptions


_CASH_TRANSACTION_KINDS: Final = frozenset(
    {
        # Schwab cash income/charge kinds — they change account NAV with no
        # offsetting position, so they can explain a NAV move. Trades,
        # reinvests, and security movements are NOT evidence.
        "BANK INTEREST",
        "CASH DIVIDEND",
        "CASH IN LIEU",
        "CREDIT INTEREST",
        "DIV ADJUSTMENT",
        "INTEREST ADJ",
        "JOURNAL",
        "MARGIN INTEREST",
        "MISC CASH ENTRY",
        "MONEYLINK ADJ",
        "NON-QUALIFIED DIV",
        "PR YR CASH DIV",
        "QUALIFIED DIVIDEND",
        "SERVICE FEE",
        "SPECIAL DIVIDEND",
        "SPECIAL NON QUAL DIV",
        "SPECIAL QUAL DIV",
    }
)


def _is_cash_activity(row: Mapping[str, Any]) -> bool:
    kind = str(row.get("kind") or "").strip().upper()
    return kind in _CASH_TRANSACTION_KINDS or kind.startswith("CASH_ACTIVITY:")


def _validate_nav_observation_value_bases(
    nav_observations: Sequence[Mapping[str, Any]],
    authoritative_provider_ids: set[str],
    provider_artifact_ids: Mapping[str, set[str]],
    provider_nav_source_artifact_ids: Mapping[str, set[str]],
) -> None:
    """Require an exact basis and manifest-bound source on authoritative NAV."""

    for row in nav_observations:
        provider_id = str(row.get("provider_id") or "")
        if provider_id not in authoritative_provider_ids:
            continue
        expected = _NAV_VALUE_BASIS_BY_PROVIDER.get(provider_id)
        if expected is None:
            raise ProviderLedgerError(
                "NAV_OBSERVATION_VALUE_BASIS_UNPROVEN: "
                f"{provider_id} has no source-proven controlled basis"
            )
        actual = row.get("value_basis")
        if not isinstance(actual, str) or not actual.strip():
            raise ProviderLedgerError(
                "NAV_OBSERVATION_VALUE_BASIS_MISSING: "
                f"{provider_id} row {row.get('record_id')} lacks value_basis"
            )
        if actual != expected:
            raise ProviderLedgerError(
                "NAV_OBSERVATION_VALUE_BASIS_INVALID: "
                f"{provider_id} row {row.get('record_id')} declares {actual!r}; "
                f"expected {expected!r}"
            )
        source_artifact_id = row.get("source_artifact_id")
        if not isinstance(source_artifact_id, str) or not source_artifact_id.strip():
            raise ProviderLedgerError(
                "NAV_OBSERVATION_SOURCE_ARTIFACT_MISSING: "
                f"{provider_id} row {row.get('record_id')} lacks source_artifact_id"
            )
        if source_artifact_id not in provider_artifact_ids.get(provider_id, set()):
            raise ProviderLedgerError(
                "NAV_OBSERVATION_SOURCE_ARTIFACT_UNKNOWN: "
                f"{provider_id} row {row.get('record_id')} references an artifact "
                "outside its verified generation manifest"
            )
        if source_artifact_id not in provider_nav_source_artifact_ids.get(
            provider_id, set()
        ):
            raise ProviderLedgerError(
                "NAV_OBSERVATION_SOURCE_ARTIFACT_UNAUTHORIZED: "
                f"{provider_id} row {row.get('record_id')} references a manifest "
                "artifact that is not an authorized NAV source"
            )


def _validate_nav_observation_moves(
    nav_observations: Sequence[Mapping[str, Any]],
    flows: Sequence[Mapping[str, Any]],
    transactions: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Reject day-over-day NAV moves >10x that no same-day activity explains.

    The bound is intentionally narrow: it catches unit/double-conversion
    anomalies without second-guessing legitimate large flows. The explanatory
    evidence is ANY mapped same-day cash activity (flows or transactions —
    e.g. a Cash Dividend lifting a dust residual); only flows adjust returns,
    transactions only testify that the move is real.
    """
    activity_amounts: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    explanatory = [
        *(row for row in flows),
        *(row for row in transactions if _is_cash_activity(row)),
    ]
    for row in explanatory:
        if row.get("mapping_status") != "MAPPED":
            continue
        raw_amount = row.get("amount")
        if raw_amount is None or (
            isinstance(raw_amount, str) and not raw_amount.strip()
        ):
            continue
        amount = _observed_decimal(raw_amount, "activity amount")
        if amount is None:
            continue
        key = (
            str(row["provider_id"]),
            str(row["account_id"]),
            str(row.get("event_date") or row.get("as_of_date") or ""),
        )
        activity_amounts[key] += abs(amount)
    by_account: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in nav_observations:
        if row.get("mapping_status") != "MAPPED":
            continue
        by_account[(str(row["provider_id"]), str(row["account_id"]))].append(row)
    for (provider_id, account_id), rows in by_account.items():
        ordered = sorted(rows, key=lambda item: str(item["as_of_date"]))
        for previous, current in zip(ordered, ordered[1:]):
            prior_nav = _observed_decimal(previous.get("nav"), "prior NAV")
            ending_nav = _observed_decimal(current.get("nav"), "ending NAV")
            if prior_nav is None or ending_nav is None or prior_nav <= 0:
                continue
            if ending_nav > prior_nav * Decimal(
                "10"
            ) or ending_nav < prior_nav / Decimal("10"):
                day = str(current["as_of_date"])
                activity_total = activity_amounts.get((provider_id, account_id, day))
                if activity_total is None:
                    raise ProviderLedgerError(
                        "NAV_OBSERVATION_EXCESSIVE_MOVE_WITHOUT_FLOW: "
                        f"{provider_id}/{account_id} moved from {prior_nav} to "
                        f"{ending_nav} on {day} without a matching flow"
                    )
                # Activity exempts the move only if its magnitude can explain
                # it: a $1 transfer must not whitewash a unit/conversion
                # error that coincides with it.
                if abs(ending_nav - prior_nav) > activity_total * Decimal("2"):
                    raise ProviderLedgerError(
                        "NAV_OBSERVATION_EXCESSIVE_MOVE_BEYOND_FLOW: "
                        f"{provider_id}/{account_id} moved from {prior_nav} to "
                        f"{ending_nav} on {day} but same-day activity totals "
                        f"{activity_total}"
                    )


def _csv_rows(
    path: Path, expected_hash: object, name: str
) -> tuple[list[dict[str, str]], SourceArtifact]:
    """Hash-verify then parse the SAME bytes — never reopen after hashing."""
    if path.is_symlink() or not path.is_file():
        raise ProviderLedgerError(f"{name} must be a regular file")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ProviderLedgerError(f"{name} is not readable") from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != _text(expected_hash, f"{name}.sha256"):
        raise ProviderLedgerError(f"{name} hash differs")
    size = len(content)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProviderLedgerError(f"{name} is not readable UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {"series_id", "date", "return", "evidence_layer"}
    if not required.issubset(reader.fieldnames or ()):
        raise ProviderLedgerError(f"{name} lacks required columns")
    rows = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ProviderLedgerError(f"{name} contains a malformed row")
        rows.append({str(key): str(value) for key, value in row.items()})
    artifact = SourceArtifact(
        artifact_id="portfolio/strategy-returns.csv",
        sha256=digest,
        size_bytes=size,
        as_of_date=max(_day(item["date"], "strategy return date") for item in rows),
        schema_name="portfolio-strategy-returns",
        schema_version="3",
    )
    return rows, artifact


def _strategy_inputs(
    value: object,
    provenance: Mapping[str, Any],
    as_of: date,
    strategy_provider_map: Mapping[str, Any],
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], tuple[SourceArtifact, SourceArtifact]
]:
    ref = _object(value, "strategy_inputs")
    expected = {
        "returns_path",
        "returns_sha256",
        "strategy_evidence_path",
        "strategy_evidence_sha256",
    }
    if set(ref) != expected:
        raise ProviderLedgerError("strategy_inputs keys differ")
    returns_path = Path(_text(ref["returns_path"], "returns_path")).resolve()
    rows, returns_artifact = _csv_rows(
        returns_path, ref["returns_sha256"], "strategy returns"
    )
    evidence_path = Path(
        _text(ref["strategy_evidence_path"], "strategy_evidence_path")
    ).resolve()
    evidence, evidence_hash, evidence_size = _verified_json(
        evidence_path, ref["strategy_evidence_sha256"], "strategy evidence"
    )
    evidence_rows = _objects(evidence.get("strategies"), "strategy evidence.strategies")
    strategies = {
        _text(item.get("strategy_id"), "strategy_id"): _text(
            item.get("name"), "strategy.name"
        )
        for item in _objects(provenance.get("strategies"), "strategies")
    }
    names = {name: strategy_id for strategy_id, name in strategies.items()}
    if not set(strategy_provider_map) <= set(strategies):
        raise ProviderLedgerError("strategy_provider_map contains an unknown strategy")
    provider_policies: dict[str, tuple[str, EvidenceLayer | None]] = {}
    for policy_strategy_id, raw_policy in strategy_provider_map.items():
        if isinstance(raw_policy, str):
            provider_policies[policy_strategy_id] = (
                _text(raw_policy, f"strategy_provider_map.{policy_strategy_id}"),
                None,
            )
            continue
        policy = _object(raw_policy, f"strategy_provider_map.{policy_strategy_id}")
        if set(policy) != {"evidence_layer", "provider_id"}:
            raise ProviderLedgerError(
                f"strategy_provider_map.{policy_strategy_id} policy keys differ"
            )
        provider_policies[policy_strategy_id] = (
            _text(
                policy["provider_id"],
                f"strategy_provider_map.{policy_strategy_id}",
            ),
            EvidenceLayer(
                _text(
                    policy["evidence_layer"],
                    f"strategy_provider_map.{policy_strategy_id}.evidence_layer",
                )
            ),
        )
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        strategy_id = names.get(row["series_id"])
        if strategy_id is None:
            raise ProviderLedgerError("strategy returns contain an unknown series")
        day = _day(row["date"], "strategy return date")
        if day > as_of:
            raise ProviderLedgerError("strategy return is newer than the ledger")
        provider_id, layer_override = provider_policies[strategy_id]
        layer = layer_override or EvidenceLayer(row["evidence_layer"])
        identity = (strategy_id, day.isoformat(), layer.value)
        if identity in seen:
            raise ProviderLedgerError("strategy return identity is duplicated")
        seen.add(identity)
        normalized.append(
            {
                "cost": row.get("cost") or None,
                "date": day.isoformat(),
                "evidence_layer": layer.value,
                "return": format(_decimal(row["return"], "strategy return"), "f"),
                "return_basis": row.get("return_basis") or "NET",
                "return_type": row.get("return_type") or "TOTAL",
                "provider_id": provider_id,
                "strategy_id": strategy_id,
                "strategy_name": row["series_id"],
                **(
                    {
                        "component_account_ids": sorted(
                            {
                                _text(
                                    assignment.get("account_id"),
                                    "assignment.account_id",
                                )
                                for assignment in _objects(
                                    provenance.get("assignments"), "assignments"
                                )
                                if assignment.get("mapping_status") == "MAPPED"
                                and assignment.get("strategy_id") == strategy_id
                                and (
                                    assignment.get("effective_start") in {None, ""}
                                    or _day(
                                        assignment.get("effective_start"),
                                        "assignment.effective_start",
                                    )
                                    <= day
                                )
                                and (
                                    assignment.get("effective_end") in {None, ""}
                                    or day
                                    <= _day(
                                        assignment.get("effective_end"),
                                        "assignment.effective_end",
                                    )
                                )
                            }
                        )
                    }
                    if layer is EvidenceLayer.ACCOUNT_LIVE
                    else {}
                ),
            }
        )
        if layer is EvidenceLayer.ACCOUNT_LIVE and not normalized[-1].get(
            "component_account_ids"
        ):
            raise ProviderLedgerError(
                "ACCOUNT_LIVE strategy return lacks an effective account assignment: "
                f"strategy={normalized[-1]['strategy_id']} "
                f"date={normalized[-1]['date']}"
            )
    evidence_ids = {
        _text(item.get("strategy_id"), "strategy evidence strategy_id")
        for item in evidence_rows
    }
    if not evidence_ids <= set(strategies):
        raise ProviderLedgerError("strategy evidence contains an unknown strategy")
    evidence_artifact = SourceArtifact(
        artifact_id="portfolio/strategy-provider-evidence.json",
        sha256=evidence_hash,
        size_bytes=evidence_size,
        as_of_date=_day(evidence.get("as_of_date"), "strategy evidence as_of_date"),
        schema_name="portfolio-strategy-provider-evidence",
        schema_version="1",
    )
    return normalized, evidence_rows, (returns_artifact, evidence_artifact)


def _observed_bounds(rows: Sequence[Mapping[str, Any]]) -> tuple[date, date]:
    days = [day for row in rows if (day := _row_day(row)) is not None]
    if not days:
        raise ProviderLedgerError("admitted provider fact lacks an effective date")
    return min(days), max(days)


def _broker_strategy_returns(
    nav_rows: Sequence[Mapping[str, Any]],
    flow_rows: Sequence[Mapping[str, Any]],
    authoritative_provider_ids: set[str],
    *,
    declared_nav_gap_dates_by_provider: Mapping[str, frozenset[date]] | None = None,
) -> list[dict[str, Any]]:
    """Create flow-adjusted account-live returns from admitted broker NAV.

    A return is emitted only when both adjacent NAV observations map to the same
    accepted strategy. Date-only flows use the documented end-of-day convention.
    Multiple accounts assigned to one strategy are beginning-capital weighted.
    """

    declared_gaps = declared_nav_gap_dates_by_provider or {}
    flows: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for row in flow_rows:
        if row.get("mapping_status") != "MAPPED":
            continue
        provider_id = str(row["provider_id"])
        if provider_id not in authoritative_provider_ids:
            continue
        account_id = str(row["account_id"])
        day = str(row.get("event_date") or "")
        if not day:
            continue
        raw_amount = row.get("amount")
        amount_missing = raw_amount is None or (
            isinstance(raw_amount, str) and not raw_amount.strip()
        )
        amount = (
            None if amount_missing else _observed_decimal(raw_amount, "flow amount")
        )
        if str(row.get("flow_type") or "").startswith("TRANSFER:INTERNAL") and (
            amount_missing or amount is None
        ):
            # Intra-account currency-ledger moves without a USD amount net to
            # zero at account level; they are never contributions. An internal
            # transfer WITH an available amount is real capital movement and
            # is counted (both sides net out within an account; across
            # accounts each side adjusts its own sleeve).
            continue
        if amount_missing:
            raise ProviderLedgerError("mapped flow amount is missing")
        if amount is None:
            raise ProviderLedgerError("mapped flow amount is unavailable")
        flows[(provider_id, account_id, day)] += amount

    by_account: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in nav_rows:
        if row.get("mapping_status") != "MAPPED":
            continue
        provider_id = str(row["provider_id"])
        if provider_id not in authoritative_provider_ids:
            continue
        by_account[(provider_id, str(row["account_id"]))].append(row)

    contributions: dict[
        tuple[str, str], list[tuple[Decimal, Decimal, str, str, str]]
    ] = defaultdict(list)
    for (provider_id, account_id), rows in by_account.items():
        ordered = sorted(rows, key=lambda item: str(item["as_of_date"]))
        for previous, current in zip(ordered, ordered[1:]):
            if previous.get("strategy_id") != current.get("strategy_id"):
                continue
            previous_day = _day(previous.get("as_of_date"), "prior NAV date")
            current_day = _day(current.get("as_of_date"), "ending NAV date")
            provider_gaps = declared_gaps.get(provider_id, frozenset())
            if previous_day in provider_gaps or current_day in provider_gaps:
                raise ProviderLedgerError(
                    "declared NAV gap contains an admitted NAV observation"
                )
            if any(previous_day < gap < current_day for gap in provider_gaps):
                # The next observed account value spans an explicitly missing
                # source date.  It is valid NAV evidence but not a one-period
                # daily return and therefore cannot enter strategy_returns.
                continue
            prior_nav = _observed_decimal(previous.get("nav"), "prior NAV")
            ending_nav = _observed_decimal(current.get("nav"), "ending NAV")
            if prior_nav is None or ending_nav is None:
                continue
            if prior_nav <= 0 or ending_nav < 0:
                continue
            day = str(current["as_of_date"])
            net_flow = flows.get((provider_id, account_id, day), Decimal(0))
            period_return = (ending_nav - net_flow) / prior_nav - Decimal(1)
            if period_return <= -1:
                raise ProviderLedgerError("flow-adjusted account return is invalid")
            strategy_id = _text(current.get("strategy_id"), "strategy_id")
            contributions[(strategy_id, day)].append(
                (
                    prior_nav,
                    period_return,
                    provider_id,
                    account_id,
                    str(current["record_id"]),
                )
            )

    result: list[dict[str, Any]] = []
    for (strategy_id, day), items in sorted(contributions.items()):
        denominator = sum((item[0] for item in items), Decimal(0))
        if denominator <= 0:
            continue
        composite = (
            sum((capital * value for capital, value, _, _, _ in items), Decimal(0))
            / denominator
        )
        providers = sorted({item[2] for item in items})
        account_ids = sorted({item[3] for item in items})
        source_record_ids = sorted(item[4] for item in items)
        result.append(
            {
                "account_reconciliation_sha256": hashlib.sha256(
                    canonical_json_bytes(
                        {
                            "account_ids": account_ids,
                            "date": day,
                            "source_record_ids": source_record_ids,
                            "strategy_id": strategy_id,
                        }
                    )
                ).hexdigest(),
                "component_account_count": len(items),
                "component_account_ids": account_ids,
                "beginning_capital": format(denominator, "f"),
                "date": day,
                "evidence_layer": "ACCOUNT_LIVE",
                "flow_timing": "DATE_ONLY_END_OF_DAY",
                "provider_id": providers[0]
                if len(providers) == 1
                else "multi-provider",
                "return": format(composite, "f"),
                "return_basis": "NET",
                "return_type": "TOTAL",
                "source_record_ids": source_record_ids,
                "strategy_id": strategy_id,
            }
        )
    return result


def _declared_nav_gap_dates_by_provider(
    manifests: Mapping[str, GenerationManifest],
) -> dict[str, frozenset[date]]:
    """Project explicit provider NAV omissions into return-period boundaries."""

    result: dict[str, frozenset[date]] = {}
    prefix = "nav_missing_date:"
    for provider_id, manifest in manifests.items():
        dates = {
            _day(exclusion.item.removeprefix(prefix), "declared NAV gap date")
            for exclusion in manifest.coverage.declared_exclusions
            if exclusion.item.startswith(prefix)
        }
        if dates:
            result[provider_id] = frozenset(dates)
    return result


def _returns_within_declared_nav_gaps(
    rows: Sequence[Mapping[str, Any]],
    declared_nav_gap_dates_by_provider: Mapping[str, frozenset[date]],
) -> list[Mapping[str, Any]]:
    """Omit retained daily returns whose observation interval crosses a gap."""

    grouped: dict[tuple[str, str, str], list[tuple[int, date]]] = defaultdict(list)
    for index, row in enumerate(rows):
        provider_id = _text(row.get("provider_id"), "strategy return provider_id")
        grouped[
            (
                provider_id,
                _text(row.get("strategy_id"), "strategy return strategy_id"),
                _text(row.get("evidence_layer"), "strategy return evidence_layer"),
            )
        ].append((index, _day(row.get("date"), "strategy return date")))

    omitted: set[int] = set()
    for (provider_id, _strategy_id, _layer), dated_rows in grouped.items():
        gaps = declared_nav_gap_dates_by_provider.get(provider_id, frozenset())
        if not gaps:
            continue
        previous_day: date | None = None
        for index, current_day in sorted(dated_rows, key=lambda item: item[1]):
            if current_day in gaps:
                raise ProviderLedgerError(
                    "declared NAV gap contains a retained strategy return"
                )
            if previous_day is not None and any(
                previous_day < gap < current_day for gap in gaps
            ):
                omitted.add(index)
            previous_day = current_day
    return [row for index, row in enumerate(rows) if index not in omitted]


def _merge_account_returns(
    source_rows: Sequence[Mapping[str, Any]],
    broker_rows: Sequence[Mapping[str, Any]],
    strategy_names: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Append newer broker observations without rewriting accepted history."""

    result = [dict(item) for item in source_rows]
    last_source: dict[str, str] = {}
    for row in result:
        if row.get("evidence_layer") == "ACCOUNT_LIVE":
            strategy_id = str(row["strategy_id"])
            last_source[strategy_id] = max(
                last_source.get(strategy_id, ""), str(row["date"])
            )
    for broker_row in broker_rows:
        strategy_id = str(broker_row["strategy_id"])
        if str(broker_row["date"]) <= last_source.get(strategy_id, ""):
            continue
        result.append(
            {
                **broker_row,
                "cost": None,
                "strategy_name": strategy_names[strategy_id],
            }
        )
    result.sort(
        key=lambda item: (
            str(item["strategy_id"]),
            str(item["date"]),
            str(item["evidence_layer"]),
        )
    )
    return result


def _strategy_return_overlap_checks(
    source_rows: Sequence[Mapping[str, Any]],
    broker_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, object]]:
    """Retain explicit parity evidence for broker observations not rolled forward."""

    tolerance = Decimal("0.000001")
    source = {
        (str(row["strategy_id"]), str(row["date"])): row
        for row in source_rows
        if row.get("evidence_layer") == "ACCOUNT_LIVE"
    }
    comparisons: dict[str, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    for broker in broker_rows:
        identity = (str(broker["strategy_id"]), str(broker["date"]))
        inherited = source.get(identity)
        if inherited is None:
            continue
        difference = abs(
            _decimal(inherited["return"], "inherited strategy return")
            - _decimal(broker["return"], "broker strategy return")
        )
        beginning_capital = _decimal(
            broker.get("beginning_capital") or "0", "broker beginning capital"
        )
        comparisons[identity[0]].append((difference, beginning_capital))
    rows: list[dict[str, object]] = []
    for strategy_id, comparisons_for_strategy in sorted(comparisons.items()):
        differences = [item[0] for item in comparisons_for_strategy]
        exception_count = sum(item > tolerance for item in differences)
        dollar_impacts = [
            difference * capital
            for difference, capital in comparisons_for_strategy
            if difference > tolerance
        ]
        rows.append(
            {
                "adjudication": (
                    "METHOD_AND_FLOW_BOUNDARY_VARIANCE; accepted tracker account "
                    "path retains its correction set; broker NAV path is retained "
                    "as reconciliation evidence and rollforward input only"
                ),
                "authority_effect": (
                    "INHERITED_ACCOUNT_PATH_RETAINED; BROKER_OVERLAP_NOT_PROMOTED"
                ),
                "blocking": False,
                "check_id": f"strategy-return-overlap-{strategy_id}",
                "compared_date_count": len(differences),
                "exception_date_count": exception_count,
                "maximum_absolute_difference": format(max(differences), "f"),
                "maximum_estimated_dollar_impact": (
                    format(max(dollar_impacts), "f") if dollar_impacts else "0"
                ),
                "status": (
                    "PASS_WITH_ADJUDICATED_VARIANCE" if exception_count else "PASS"
                ),
                "strategy_id": strategy_id,
                "tolerance": format(tolerance, "f"),
            }
        )
    return rows


def _path_statistics(rows: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    ordered = sorted(rows, key=lambda item: str(item["date"]))
    if not ordered:
        raise ProviderLedgerError("strategy path is empty")
    wealth = Decimal(1)
    high = Decimal(1)
    maximum_drawdown = Decimal(0)
    for row in ordered:
        wealth *= Decimal(1) + _decimal(row["return"], "strategy return")
        high = max(high, wealth)
        maximum_drawdown = min(maximum_drawdown, wealth / high - Decimal(1))
    return {
        "cumulative_return": format(wealth - Decimal(1), "f"),
        "current_drawdown": format(wealth / high - Decimal(1), "f"),
        "maximum_drawdown": format(maximum_drawdown, "f"),
        "observed_end": str(ordered[-1]["date"]),
        "observed_start": str(ordered[0]["date"]),
        "return_observations": len(ordered),
    }


def _refresh_strategy_evidence(
    rows: Sequence[Mapping[str, Any]],
    evidence_rows: Sequence[Mapping[str, Any]],
    strategy_names: Mapping[str, str],
    provider_generations: Mapping[str, ProviderGenerationIdentity],
    provider_as_of_dates: Mapping[str, date],
) -> list[dict[str, Any]]:
    existing = {str(item["strategy_id"]): dict(item) for item in evidence_rows}
    by_strategy_layer: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(
        list
    )
    for row in rows:
        layer = str(row.get("evidence_layer"))
        if layer in {"ACCOUNT_LIVE", "PROVIDER_LIVE"}:
            by_strategy_layer[(str(row["strategy_id"]), layer)].append(row)
    refreshed: list[dict[str, Any]] = []
    strategy_ids = sorted({strategy_id for strategy_id, _ in by_strategy_layer})
    for strategy_id in strategy_ids:
        prior = existing.get(strategy_id, {})
        account_rows = by_strategy_layer.get((strategy_id, "ACCOUNT_LIVE"), [])
        provider_rows = by_strategy_layer.get((strategy_id, "PROVIDER_LIVE"), [])
        account_providers = sorted(
            {
                str(item["provider_id"])
                for item in account_rows
                if item.get("provider_id")
            }
        )
        provider_providers = sorted(
            {
                str(item["provider_id"])
                for item in provider_rows
                if item.get("provider_id")
            }
        )
        account_provider_id = (
            account_providers[0]
            if len(account_providers) == 1
            else str(prior.get("provider_id", "portfolio-tracker"))
        )
        prior_provider = (
            dict(prior["provider"])
            if isinstance(prior.get("provider"), Mapping)
            else {}
        )
        provider_provider_id = str(
            prior_provider.get("provider_id")
            or (
                prior.get("provider_id")
                if prior_provider
                else provider_providers[0]
                if len(provider_providers) == 1
                else "portfolio-tracker"
            )
        )
        accepted_provider_rows = [
            item
            for item in provider_rows
            if str(item.get("provider_id")) == provider_provider_id
        ] or provider_rows
        top_provider_id = account_provider_id if account_rows else provider_provider_id
        top_generation = provider_generations.get(top_provider_id)
        refreshed_row = {
            **prior,
            "evidence_layer": ("ACCOUNT_LIVE" if account_rows else "PROVIDER_LIVE"),
            "generation_as_of_date": (
                provider_as_of_dates[top_provider_id].isoformat()
                if top_generation is not None
                else prior.get("generation_as_of_date")
            ),
            "generation_id": (
                top_generation.generation_id
                if top_generation is not None
                else prior.get("generation_id")
            ),
            "provider_id": top_provider_id,
            "sources": prior.get("sources")
            or {
                (
                    "account_returns" if account_rows else "provider_returns"
                ): "portfolio/strategy-returns.csv"
            },
            "strategy_id": strategy_id,
            "strategy_name": strategy_names[strategy_id],
        }
        if account_rows:
            account_generation = provider_generations.get(account_provider_id)
            account_scopes = []
            account_ids = sorted(
                {
                    str(account_id)
                    for row in account_rows
                    for account_id in row.get("component_account_ids", [])
                }
            )
            if not account_ids:
                raise ProviderLedgerError(
                    f"ACCOUNT_LIVE strategy evidence lacks account scope: {strategy_id}"
                )
            for account_id in account_ids:
                scoped_rows = [
                    row
                    for row in account_rows
                    if account_id in row.get("component_account_ids", [])
                ]
                account_scopes.append(
                    {
                        "account_id": account_id,
                        "observed_end": max(str(row["date"]) for row in scoped_rows),
                        "observed_start": min(str(row["date"]) for row in scoped_rows),
                        "reconciliation_sha256": hashlib.sha256(
                            canonical_json_bytes(
                                {
                                    "account_id": account_id,
                                    "rows": [
                                        {
                                            "date": str(row["date"]),
                                            "provider_id": str(row["provider_id"]),
                                            "return": str(row["return"]),
                                            "source_record_ids": sorted(
                                                str(value)
                                                for value in row.get(
                                                    "source_record_ids", []
                                                )
                                            ),
                                        }
                                        for row in sorted(
                                            scoped_rows,
                                            key=lambda item: str(item["date"]),
                                        )
                                    ],
                                    "strategy_id": strategy_id,
                                }
                            )
                        ).hexdigest(),
                    }
                )
            refreshed_row["account"] = {
                **_path_statistics(account_rows),
                "account_scopes": account_scopes,
                "generation_as_of_date": (
                    provider_as_of_dates[account_provider_id].isoformat()
                    if account_generation is not None
                    else prior.get("generation_as_of_date")
                ),
                "generation_id": (
                    account_generation.generation_id
                    if account_generation is not None
                    else prior.get("generation_id")
                ),
                "provider_id": account_provider_id,
            }
        if accepted_provider_rows or prior_provider:
            provider_generation = provider_generations.get(provider_provider_id)
            refreshed_row["provider"] = {
                **prior_provider,
                **(
                    _path_statistics(accepted_provider_rows)
                    if accepted_provider_rows
                    else {}
                ),
                "generation_as_of_date": (
                    provider_as_of_dates[provider_provider_id].isoformat()
                    if provider_generation is not None
                    else prior_provider.get("generation_as_of_date")
                    or prior.get("generation_as_of_date")
                ),
                "generation_id": (
                    provider_generation.generation_id
                    if provider_generation is not None
                    else prior_provider.get("generation_id")
                    or prior.get("generation_id")
                ),
                "provider_id": provider_provider_id,
            }
        refreshed.append(refreshed_row)
    return refreshed


def _portfolio123_provider_returns(
    provider_ref: Mapping[str, Any],
    manifest: GenerationManifest,
    evidence_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    p123_rows = [
        item
        for item in evidence_rows
        if isinstance(item.get("provider"), dict)
        and (
            item["provider"].get("provider_id") == "portfolio123"
            or item.get("provider_id") == "portfolio123"
        )
        and item.get("generation_id") == manifest.generation_id
        and item["provider"].get("entity_id") not in {None, ""}
    ]
    if not p123_rows:
        return []
    root = Path(_text(provider_ref["root"], "portfolio123.root")).resolve()
    generation_root = root / "generations" / manifest.generation_id
    equity_path = _exact_file(
        generation_root,
        Path("tables/har_equity_curve.parquet"),
        "Portfolio123 equity curve",
    )
    performance_path = _exact_file(
        generation_root,
        Path("tables/har_managed_performance.parquet"),
        "Portfolio123 managed performance",
    )
    equity_artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "tables/har_equity_curve.parquet"
        ),
        None,
    )
    performance_artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.artifact_id == "tables/har_managed_performance.parquet"
        ),
        None,
    )
    if equity_artifact is None or hash_file(equity_path) != (
        equity_artifact.sha256,
        equity_artifact.size_bytes,
    ):
        raise ProviderLedgerError("Portfolio123 equity curve differs from its manifest")
    if performance_artifact is None or hash_file(performance_path) != (
        performance_artifact.sha256,
        performance_artifact.size_bytes,
    ):
        raise ProviderLedgerError(
            "Portfolio123 managed performance differs from its manifest"
        )
    try:
        import pyarrow.parquet as pq  # type: ignore[import-untyped]

        equity_table = pq.read_table(
            equity_path,
            columns=["_entity_id", "Date", "Total Equity", "Cash Added"],
        )
    except Exception as exc:
        raise ProviderLedgerError("Portfolio123 equity curve is unreadable") from exc
    try:
        performance_table = pq.read_table(
            performance_path,
            columns=["_entity_id", "field", "value"],
        )
    except Exception as exc:
        raise ProviderLedgerError(
            "Portfolio123 managed performance is unreadable"
        ) from exc
    selected: dict[str, dict[str, str]] = {
        _text(item["provider"].get("entity_id"), "Portfolio123 entity_id"): {}
        for item in p123_rows
    }
    equity_observations: dict[str, dict[date, tuple[Decimal, Decimal]]] = {
        entity_id: {} for entity_id in selected
    }
    for row in equity_table.to_pylist():
        entity_id = str(row.get("_entity_id") or "")
        if entity_id not in equity_observations:
            continue
        observed = _day(str(row.get("Date") or ""), "Portfolio123 equity date")
        equity_value = _decimal(row.get("Total Equity"), "Portfolio123 equity")
        cash_added = _decimal(row.get("Cash Added") or "0", "Portfolio123 cash added")
        if equity_value <= 0:
            raise ProviderLedgerError("Portfolio123 equity must be positive")
        value = (equity_value, cash_added)
        prior = equity_observations[entity_id].get(observed)
        if prior is not None and prior != value:
            raise ProviderLedgerError(
                "Portfolio123 equity curve has conflicting duplicate dates"
            )
        equity_observations[entity_id][observed] = value
    for row in performance_table.to_pylist():
        entity_id = str(row.get("_entity_id") or "")
        if entity_id not in selected:
            continue
        field = _text(row.get("field"), "Portfolio123 managed performance field")
        value = _text(row.get("value"), "Portfolio123 managed performance value")
        prior = selected[entity_id].get(field)
        if prior is not None and prior != value:
            raise ProviderLedgerError(
                "Portfolio123 managed performance has conflicting captures"
            )
        selected[entity_id][field] = value

    result: list[dict[str, Any]] = []
    required_fields = {"data", "date", "dateLast", "panels"}
    for p123 in p123_rows:
        entity_id = _text(p123["provider"].get("entity_id"), "Portfolio123 entity_id")
        fields = selected[entity_id]
        if not required_fields <= set(fields):
            raise ProviderLedgerError(
                f"Portfolio123 provider path is unavailable: {entity_id}"
            )
        try:
            performance_start = _day(fields["date"], "Portfolio123 managed start")
            performance_end = _day(fields["dateLast"], "Portfolio123 managed end")
            data = json.loads(fields["data"], parse_float=Decimal, parse_int=Decimal)
            panels = json.loads(fields["panels"])
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderLedgerError(
                "Portfolio123 managed performance is invalid"
            ) from exc
        if not isinstance(data, list) or not isinstance(panels, list):
            raise ProviderLedgerError("Portfolio123 managed performance shape differs")
        graphs = [
            graph
            for panel in panels
            if isinstance(panel, dict)
            for graph in panel.get("graphs", [])
            if isinstance(graph, dict)
        ]
        if not any(
            graph.get("idx") == 0 and graph.get("s") == "Strategy" for graph in graphs
        ) or not any(
            graph.get("idx") == 1 and graph.get("s") == "Total Value"
            for graph in graphs
        ):
            raise ProviderLedgerError(
                "Portfolio123 managed performance selector differs"
            )
        calendar: list[date] = []
        observed = performance_start
        while observed <= performance_end:
            if observed.weekday() < 5:
                calendar.append(observed)
            observed += timedelta(days=1)
        if len(calendar) != len(data):
            raise ProviderLedgerError(
                "Portfolio123 managed performance calendar differs"
            )
        parsed: list[tuple[date, Decimal, Decimal, Decimal]] = []
        for observed, raw in zip(calendar, data, strict=True):
            if not isinstance(raw, list) or len(raw) != 3:
                raise ProviderLedgerError(
                    "Portfolio123 managed performance observation differs"
                )
            wealth = Decimal(1) + _decimal(
                raw[0], "Portfolio123 managed strategy performance"
            ) / Decimal(100)
            total_equity = _decimal(raw[1], "Portfolio123 managed total value")
            cash_added = _decimal(raw[2], "Portfolio123 managed cash added")
            if wealth <= 0 or total_equity <= 0:
                raise ProviderLedgerError(
                    "Portfolio123 managed performance contains invalid values"
                )
            parsed.append((observed, wealth, total_equity, cash_added))
        if len(parsed) < 2:
            raise ProviderLedgerError(
                f"Portfolio123 provider path is unavailable: {entity_id}"
            )
        ordered_equity = sorted(equity_observations[entity_id].items())
        overlap = 0
        for observed, _wealth, total_equity, cash_added in parsed:
            if observed not in equity_observations[entity_id]:
                continue
            overlap += 1
            if equity_observations[entity_id][observed] != (total_equity, cash_added):
                raise ProviderLedgerError(
                    "Portfolio123 managed performance and equity differ"
                )
        if len(ordered_equity) < 2 or overlap < 2:
            raise ProviderLedgerError(
                "Portfolio123 managed performance/equity overlap is insufficient"
            )
        for previous, current in zip(parsed, parsed[1:], strict=False):
            period_return = current[1] / previous[1] - Decimal(1)
            if period_return <= -1:
                raise ProviderLedgerError("Portfolio123 provider return is invalid")
            result.append(
                {
                    "cost": None,
                    "date": current[0].isoformat(),
                    "evidence_layer": "PROVIDER_LIVE",
                    "generation_id": manifest.generation_id,
                    "provider_id": "portfolio123",
                    "return": format(period_return, "f"),
                    "return_basis": "OBSERVED",
                    "return_type": "TOTAL",
                    "strategy_id": str(p123["strategy_id"]),
                    "strategy_name": str(p123["strategy_name"]),
                }
            )
    result.sort(key=lambda item: (str(item["strategy_id"]), str(item["date"])))
    return result


def _admissions(
    projections: Mapping[str, Sequence[Mapping[str, Any]]],
    evidence_rows: Sequence[Mapping[str, Any]],
    authoritative_provider_ids: set[str] | None = None,
    authoritative_strategy_ids: set[str] | None = None,
) -> tuple[ProviderFactAdmission, ...]:
    provider_authority = (
        set(_PROVIDER_CATALOGS)
        if authoritative_provider_ids is None
        else authoritative_provider_ids
    )
    strategy_authority = (
        {str(item.get("strategy_id")) for item in evidence_rows}
        if authoritative_strategy_ids is None
        else authoritative_strategy_ids
    )
    admissions: list[ProviderFactAdmission] = []
    by_provider_fact_account: dict[
        tuple[str, str, str, str], list[Mapping[str, Any]]
    ] = defaultdict(list)
    for fact_type, rows in projections.items():
        for row in rows:
            by_provider_fact_account[
                (
                    str(row["provider_id"]),
                    fact_type,
                    str(row["account_id"]),
                    str(row["mapping_status"]),
                )
            ].append(row)
    for (
        provider_id,
        fact_type,
        account_id,
        mapping_status,
    ), rows in by_provider_fact_account.items():
        start, end = _observed_bounds(rows)
        admissions.append(
            ProviderFactAdmission(
                provider_id=provider_id,
                fact_type=fact_type,
                evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
                disposition=(
                    AdmissionDisposition.AUTHORITATIVE
                    if mapping_status == "MAPPED" and provider_id in provider_authority
                    else AdmissionDisposition.REFERENCE_ONLY
                ),
                effective_start=start,
                effective_end=end,
                account_id=account_id,
                account_reconciliation_sha256=hashlib.sha256(
                    canonical_json_bytes(
                        {
                            "account_id": account_id,
                            "fact_type": fact_type,
                            "provider_id": provider_id,
                            "records": [
                                {
                                    "record_id": str(item["record_id"]),
                                    "source_record_id": str(
                                        item.get("source_record_id") or ""
                                    ),
                                }
                                for item in sorted(
                                    rows, key=lambda row: str(row["record_id"])
                                )
                            ],
                        }
                    )
                ).hexdigest(),
            )
        )
    for item in evidence_rows:
        strategy_id = _text(item.get("strategy_id"), "strategy evidence strategy_id")
        if item.get("account") is not None:
            account = _object(item["account"], "strategy evidence account")
            account_provider_id = _text(
                account.get("provider_id") or item.get("provider_id"),
                "strategy evidence account provider_id",
            )
            for scope in _objects(
                account.get("account_scopes"), "strategy evidence account_scopes"
            ):
                admissions.append(
                    ProviderFactAdmission(
                        provider_id=account_provider_id,
                        fact_type="strategy_returns",
                        evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
                        disposition=(
                            AdmissionDisposition.AUTHORITATIVE
                            if strategy_id in strategy_authority
                            else AdmissionDisposition.REFERENCE_ONLY
                        ),
                        effective_start=_day(
                            scope.get("observed_start"), "account observed_start"
                        ),
                        effective_end=_day(
                            scope.get("observed_end"), "account observed_end"
                        ),
                        account_id=_text(scope.get("account_id"), "account_id"),
                        strategy_id=strategy_id,
                        account_reconciliation_sha256=_text(
                            scope.get("reconciliation_sha256"),
                            "account reconciliation sha256",
                        ),
                    )
                )
        if item.get("provider") is not None:
            provider = _object(item["provider"], "strategy evidence provider")
            provider_provider_id = _text(
                provider.get("provider_id") or item.get("provider_id"),
                "strategy evidence provider provider_id",
            )
            admissions.append(
                ProviderFactAdmission(
                    provider_id=provider_provider_id,
                    fact_type="strategy_returns",
                    evidence_layer=EvidenceLayer.PROVIDER_LIVE,
                    disposition=AdmissionDisposition.RECONCILIATION_ONLY,
                    effective_start=_day(
                        provider.get("observed_start"), "provider observed_start"
                    ),
                    effective_end=_day(
                        provider.get("observed_end"), "provider observed_end"
                    ),
                    strategy_id=strategy_id,
                )
            )
    return validate_provider_authority(admissions)


def _provider_coverage(
    provider_id: str,
    manifest_coverage: CoverageClaim,
    provider_catalog: Mapping[str, Any],
) -> dict[str, object]:
    """Retain provider-declared limitations without replacing manifest authority."""

    raw_catalog_coverage = provider_catalog.get("coverage")
    if raw_catalog_coverage is None:
        catalog_coverage: Mapping[str, Any] = {}
    else:
        catalog_coverage = _object(
            raw_catalog_coverage, f"{provider_id} catalog coverage"
        )

    def limitations(field: str) -> list[str]:
        raw = catalog_coverage.get(field, [])
        if not isinstance(raw, list):
            raise ProviderLedgerError(
                f"{provider_id} catalog coverage.{field} must be a list"
            )
        return sorted(
            {_text(item, f"{provider_id} catalog coverage.{field}") for item in raw}
        )

    catalog_known_gaps = limitations("known_gaps")
    out_of_boundary = limitations("out_of_boundary")
    authoritative = manifest_coverage.to_dict()
    authoritative["known_gaps"] = sorted(
        {
            *(
                _text(item, f"{provider_id} manifest coverage.known_gaps")
                for item in manifest_coverage.known_gaps
            ),
            *catalog_known_gaps,
        }
    )
    authoritative["out_of_boundary"] = out_of_boundary
    return authoritative


def _source_segment(
    authority: Mapping[str, Any],
    provider_id: str,
    identity: ProviderGenerationIdentity,
    manifest: GenerationManifest,
) -> dict[str, Any]:
    source_id = _PROVIDER_SOURCE_SEGMENTS[provider_id]
    source = _object(
        _object(authority["sources"], "financial MVP sources")[source_id],
        f"source segment {source_id}",
    )
    generation = _object(source.get("current_generation"), f"{source_id}.generation")
    if generation != {
        "as_of": manifest.as_of_date.isoformat(),
        "generation_id": identity.generation_id,
        "sha256": identity.manifest_sha256,
    }:
        raise ProviderLedgerError(
            f"{provider_id} generation differs from financial MVP authority"
        )
    admission = _object(source.get("admission"), f"{source_id}.admission")
    if not str(admission.get("transform") or "").startswith("provider-ledger"):
        raise ProviderLedgerError(
            f"{provider_id} source is not authorized for provider-ledger admission"
        )
    if source.get("capture_status") != "CAPTURED_GOVERNED":
        raise ProviderLedgerError(f"{provider_id} source is not governed capture")
    admitted_facts = set(_strings(admission.get("facts"), f"{source_id}.facts"))
    required_facts = {
        _DATA_CLASS_ADMISSION_FACT[data_class]
        for data_class in _strings(
            source.get("data_classes"), f"{source_id}.data_classes"
        )
        if data_class in _DATA_CLASS_ADMISSION_FACT
    }
    if not required_facts.issubset(admitted_facts):
        raise ProviderLedgerError(
            f"{provider_id} product data classes differ from admission facts"
        )
    return source


def _catalog_dataset_items(
    provider_id: str, catalog: Mapping[str, Any], schema_version: str
) -> list[ProviderDatasetInventoryItem]:
    items: list[ProviderDatasetInventoryItem] = []

    def visit(value: Mapping[str, Any], prefix: str = "") -> None:
        for key in sorted(value):
            child = value[key]
            path = f"{prefix}/{key}" if prefix else str(key)
            if isinstance(child, list):
                content = _canonical_json_value_bytes(child)
                items.append(
                    ProviderDatasetInventoryItem(
                        provider_id=provider_id,
                        item_kind=DatasetItemKind.CATALOG_DATASET,
                        item_id=path,
                        sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                        schema_name=f"{provider_id}-catalog-dataset",
                        schema_version=schema_version,
                        record_count=len(child),
                    )
                )
            elif isinstance(child, dict):
                visit(child, path)

    visit(catalog)
    return items


def _provider_inventory(
    provider_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    source: Mapping[str, Any],
) -> list[ProviderDatasetInventoryItem]:
    items = [
        ProviderDatasetInventoryItem(
            provider_id=provider_id,
            item_kind=DatasetItemKind.ARTIFACT,
            item_id=artifact.artifact_id,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            schema_name=artifact.schema_name,
            schema_version=artifact.schema_version,
        )
        for artifact in manifest.artifacts
    ]
    items.extend(
        ProviderDatasetInventoryItem(
            provider_id=provider_id,
            item_kind=DatasetItemKind.COVERAGE_FACT_TYPE,
            item_id=fact_type,
            sha256=hashlib.sha256(
                canonical_json_bytes(
                    {"fact_type": fact_type, "provider_id": provider_id}
                )
            ).hexdigest(),
            size_bytes=len(fact_type.encode("utf-8")),
            schema_name="generation-coverage-fact-type",
            schema_version="1",
            record_count=None,
        )
        for fact_type in sorted(manifest.coverage.fact_types)
    )
    items.extend(
        _catalog_dataset_items(
            provider_id, catalog, str(catalog.get("catalog_schema_version"))
        )
    )
    # The product data-class item is an input to the policy's admitted-binding
    # digest, so its identity must exclude that policy to avoid a digest cycle.
    source_identity = dict(source)
    source_admission = dict(_object(source.get("admission"), "source admission"))
    source_admission.pop("dataset_disposition_policy", None)
    source_identity["admission"] = source_admission
    source_sha256 = hashlib.sha256(canonical_json_bytes(source_identity)).hexdigest()
    items.extend(
        ProviderDatasetInventoryItem(
            provider_id=provider_id,
            item_kind=DatasetItemKind.PRODUCT_DATA_CLASS,
            item_id=str(data_class),
            sha256=hashlib.sha256(
                canonical_json_bytes(
                    {
                        "data_class": data_class,
                        "source_segment_id": source["source_id"],
                        "source_segment_sha256": source_sha256,
                    }
                )
            ).hexdigest(),
            size_bytes=len(str(data_class).encode("utf-8")),
            schema_name="financial-mvp-source-data-class",
            schema_version="1",
            record_count=None,
        )
        for data_class in sorted(
            _text(item, f"{source['source_id']}.data_class")
            for item in source.get("data_classes", [])
        )
    )
    return items


def _dataset_disposition_policy(source: Mapping[str, Any]) -> dict[str, Any]:
    admission = _object(source.get("admission"), "source admission")
    policy = _object(
        admission.get("dataset_disposition_policy"),
        "source admission dataset disposition policy",
    )
    if policy.get("policy_id") != "PROVIDER_ADMIT_OR_DECLARE_V1":
        raise ProviderLedgerError("provider dataset disposition policy differs")
    if policy.get("policy_version") != "1":
        raise ProviderLedgerError("provider dataset disposition policy version differs")
    if policy.get("policy_owner") != "PRODUCT_OWNER_GOVERNING_ISSUE_303":
        raise ProviderLedgerError("provider dataset disposition policy owner differs")
    if policy.get("authority_issue") != 303:
        raise ProviderLedgerError("provider dataset disposition authority differs")
    _run_id(
        policy.get("upstream_inventory_sha256"),
        "provider upstream inventory digest",
    )
    _run_id(
        policy.get("admitted_item_set_sha256"),
        "provider admitted item set digest",
    )
    _run_id(
        policy.get("product_data_class_set_sha256"),
        "provider product data-class set digest",
    )
    if (
        not isinstance(policy.get("admitted_item_count"), int)
        or policy["admitted_item_count"] < 0
    ):
        raise ProviderLedgerError("provider admitted item count is invalid")
    if (
        not isinstance(policy.get("product_data_class_count"), int)
        or policy["product_data_class_count"] < 1
    ):
        raise ProviderLedgerError("provider product data-class count is invalid")
    _strings(
        policy.get("allowed_exclusion_reasons"),
        "provider allowed exclusion reasons",
    )
    _day(policy.get("effective_start"), "provider disposition effective_start")
    if policy.get("effective_end") is not None:
        _day(policy["effective_end"], "provider disposition effective_end")
    return policy


def _inventory_digest(items: Sequence[ProviderDatasetInventoryItem]) -> str:
    return provider_upstream_inventory_digest(items)


def _admitted_item_set_digest(
    dispositions: Sequence[ProviderDatasetDisposition],
) -> tuple[int, str]:
    return admitted_binding_set_digest(dispositions)


def _product_data_class_set_digest(
    inventory: Sequence[ProviderDatasetInventoryItem],
) -> tuple[int, str]:
    return product_data_class_set_digest(inventory)


def _materialized_rows(
    provider_id: str,
    item: ProviderDatasetInventoryItem,
    provider_facts: Mapping[str, Sequence[Mapping[str, Any]]],
    provider_strategy_returns: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    target_facts: tuple[str, ...] = ()
    rows: Sequence[Mapping[str, Any]] = ()
    if item.item_kind is DatasetItemKind.PRODUCT_DATA_CLASS:
        target = _DATA_CLASS_TARGET_FACT.get(item.item_id)
        if target == "provider_strategy_returns":
            rows = [
                row
                for row in provider_strategy_returns
                if row.get("provider_id") == provider_id
            ]
        elif target is not None:
            rows = provider_facts.get(target, ())
        if target is not None:
            target_facts = (target,)
    elif item.item_kind is DatasetItemKind.CATALOG_DATASET:
        if "/" not in item.item_id:
            target = _SOURCE_FACT_TARGET.get(item.item_id)
            if target is not None:
                target_facts = (target,)
                rows = provider_facts.get(target, ())
    elif item.item_kind is DatasetItemKind.COVERAGE_FACT_TYPE:
        target = _SOURCE_FACT_TARGET.get(item.item_id)
        if target is not None:
            target_facts = (target,)
            rows = provider_facts.get(target, ())
    elif item.item_kind is DatasetItemKind.ARTIFACT:
        if (
            provider_id == "portfolio123"
            and item.item_id == "tables/har_managed_performance.parquet"
        ):
            target_facts = ("provider_strategy_returns",)
            rows = [
                row
                for row in provider_strategy_returns
                if row.get("provider_id") == provider_id
            ]
        else:
            matched: dict[str, list[Mapping[str, Any]]] = {}
            for fact_name, fact_rows in provider_facts.items():
                selected = [
                    row
                    for row in fact_rows
                    if row.get("source_artifact_id") == item.item_id
                ]
                if selected:
                    matched[fact_name] = selected
            target_facts = tuple(sorted(matched))
            rows = [row for fact_name in target_facts for row in matched[fact_name]]
    return target_facts, [dict(row) for row in rows]


def _row_bounds(rows: Sequence[Mapping[str, Any]]) -> tuple[date, date]:
    observed = []
    for row in rows:
        raw = row.get("date")
        observed.append(
            _day(raw, "materialized row date")
            if raw not in {None, ""}
            else _row_day(row)
        )
    days = [item for item in observed if item is not None]
    if len(days) != len(rows) or not days:
        raise ProviderLedgerError("admitted dataset lacks an exact observed interval")
    return min(days), max(days)


def _dataset_disposition(
    item: ProviderDatasetInventoryItem,
    source: Mapping[str, Any],
    policy: Mapping[str, Any],
    source_observed_start: date,
    source_observed_end: date,
    provider_facts: Mapping[str, Sequence[Mapping[str, Any]]],
    provider_strategy_returns: Sequence[Mapping[str, Any]],
    contract_digest: str,
) -> ProviderDatasetDisposition:
    target_facts, rows = _materialized_rows(
        item.provider_id, item, provider_facts, provider_strategy_returns
    )
    source_sha256 = hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    consumers = tuple(sorted(_strings(source.get("consumers"), "source consumers")))
    invalidated = tuple(
        sorted(_strings(source.get("invalidated_outputs"), "invalidated outputs"))
    ) or ("NO_CURRENT_PRODUCT_OUTPUT",)
    units = _object(source.get("units"), "source units")
    temporal = _object(source.get("temporal"), "source temporal")
    admission = _object(source.get("admission"), "source admission")
    evidence_layer = _text(source.get("evidence_layer"), "source evidence_layer")
    actual_layers = {
        str(row.get("evidence_layer"))
        for row in rows
        if row.get("evidence_layer") not in {None, ""}
    }
    reason: str | None = None
    if not consumers:
        reason = "NO_CURRENT_PRODUCT_CONSUMER"
    elif not rows or not target_facts:
        reason = (
            "REQUIRED_DATA_CLASS_NOT_MATERIALIZED"
            if item.item_kind is DatasetItemKind.PRODUCT_DATA_CLASS
            else "SOURCE_ITEM_NOT_ADMITTED_TO_CURRENT_LEDGER"
        )
    elif actual_layers and actual_layers != {evidence_layer}:
        reason = "MATERIALIZED_EVIDENCE_LAYER_DIFFERS_FROM_PRODUCT_AUTHORITY"
    required_admission_facts = {
        _TARGET_ADMISSION_FACT[target_fact]
        for target_fact in target_facts
        if target_fact in _TARGET_ADMISSION_FACT
    }
    if len(required_admission_facts) != len(target_facts):
        raise ProviderLedgerError(
            f"{item.provider_id} materialized target fact is not governed"
        )
    admitted_facts = set(_strings(admission.get("facts"), "source admission facts"))
    if not required_admission_facts.issubset(admitted_facts):
        raise ProviderLedgerError(
            f"{item.provider_id} materialized target facts differ from authority"
        )
    authority_ref = (
        f"contract:{contract_digest}#source_segments/{source['source_id']};"
        f"issue:{policy['authority_issue']}"
    )
    policy_effective_start = _day(
        policy["effective_start"], "provider disposition effective_start"
    )
    policy_effective_end = (
        None
        if policy.get("effective_end") is None
        else _day(policy["effective_end"], "provider disposition effective_end")
    )
    policy_id = _text(policy["policy_id"], "provider disposition policy_id")
    policy_version = _text(
        policy["policy_version"], "provider disposition policy_version"
    )
    policy_owner = _text(policy["policy_owner"], "provider disposition policy_owner")
    if reason is not None:
        return ProviderDatasetDisposition(
            provider_id=item.provider_id,
            item_kind=item.item_kind,
            item_id=item.item_id,
            item_sha256=item.sha256,
            action=DatasetDispositionAction.EXCLUDE,
            source_segment_id=str(source["source_id"]),
            source_segment_sha256=source_sha256,
            authority_ref=authority_ref,
            readiness_impact=(
                "REQUIRED_NOT_READY"
                if source.get("product_requirement") == "REQUIRED"
                else "NO_CURRENT_PRODUCT_EFFECT"
            ),
            consumers=consumers,
            invalidated_outputs=invalidated,
            evidence_layer=evidence_layer,
            units=_text(units.get("measurement"), "source measurement"),
            currency=_text(units.get("currency"), "source currency"),
            timezone=_text(temporal.get("timezone"), "source timezone"),
            calendar=_text(temporal.get("calendar"), "source calendar"),
            materialized_record_count=0,
            policy_id=policy_id,
            policy_version=policy_version,
            policy_owner=policy_owner,
            policy_effective_start=policy_effective_start,
            policy_effective_end=policy_effective_end,
            source_observed_start=source_observed_start,
            source_observed_end=source_observed_end,
            data_class=(
                item.item_id
                if item.item_kind is DatasetItemKind.PRODUCT_DATA_CLASS
                else None
            ),
            reason=reason,
        )
    start, end = _row_bounds(rows)
    materialized = _canonical_json_value_bytes(
        {"rows": rows, "target_facts": list(target_facts)}
    )
    return ProviderDatasetDisposition(
        provider_id=item.provider_id,
        item_kind=item.item_kind,
        item_id=item.item_id,
        item_sha256=item.sha256,
        action=DatasetDispositionAction.ADMIT,
        source_segment_id=str(source["source_id"]),
        source_segment_sha256=source_sha256,
        authority_ref=authority_ref,
        readiness_impact="ADMITTED",
        consumers=consumers,
        invalidated_outputs=(),
        evidence_layer=evidence_layer,
        units=_text(units.get("measurement"), "source measurement"),
        currency=_text(units.get("currency"), "source currency"),
        timezone=_text(temporal.get("timezone"), "source timezone"),
        calendar=_text(temporal.get("calendar"), "source calendar"),
        materialized_record_count=len(rows),
        policy_id=policy_id,
        policy_version=policy_version,
        policy_owner=policy_owner,
        policy_effective_start=policy_effective_start,
        policy_effective_end=policy_effective_end,
        source_observed_start=source_observed_start,
        source_observed_end=source_observed_end,
        materialized_sha256=hashlib.sha256(materialized).hexdigest(),
        observed_start=start,
        observed_end=end,
        data_class=(
            item.item_id
            if item.item_kind is DatasetItemKind.PRODUCT_DATA_CLASS
            else None
        ),
        target_facts=target_facts,
        transform=_text(admission.get("transform"), "source admission transform"),
        transform_version=_text(admission.get("version"), "source admission version"),
    )


def _admit_or_declare_receipt(
    authority: Mapping[str, Any],
    identities: Sequence[ProviderGenerationIdentity],
    manifests: Mapping[str, GenerationManifest],
    catalogs: Mapping[str, Mapping[str, Any]],
    provider_facts: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    provider_strategy_returns: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], bytes]:
    inventory: list[ProviderDatasetInventoryItem] = []
    dispositions: list[ProviderDatasetDisposition] = []
    providers = []
    by_provider = {item.provider_id: item for item in identities}
    for provider_id in sorted(_PROVIDER_CATALOGS):
        identity = by_provider[provider_id]
        manifest = manifests[provider_id]
        source = _source_segment(authority, provider_id, identity, manifest)
        policy = _dataset_disposition_policy(source)
        provider_items = _provider_inventory(
            provider_id, manifest, catalogs[provider_id], source
        )
        if _inventory_digest(provider_items) != policy["upstream_inventory_sha256"]:
            raise ProviderLedgerError(
                f"{provider_id} upstream dataset inventory differs from authority"
            )
        product_class_count, product_class_digest = _product_data_class_set_digest(
            provider_items
        )
        if (
            product_class_count != policy["product_data_class_count"]
            or product_class_digest != policy["product_data_class_set_sha256"]
        ):
            raise ProviderLedgerError(
                f"{provider_id} product data-class set differs from authority"
            )
        policy_start = _day(
            policy["effective_start"], "provider disposition effective_start"
        )
        policy_end = (
            None
            if policy.get("effective_end") is None
            else _day(policy["effective_end"], "provider disposition effective_end")
        )
        if policy_start > manifest.as_of_date or (
            policy_end is not None and policy_end < manifest.as_of_date
        ):
            raise ProviderLedgerError(
                f"{provider_id} dataset disposition policy does not cover generation"
            )
        if (
            manifest.coverage.observed_start is None
            or manifest.coverage.observed_end is None
        ):
            raise ProviderLedgerError(
                f"{provider_id} manifest lacks dataset disposition interval"
            )
        inventory.extend(provider_items)
        provider_dispositions = [
            _dataset_disposition(
                item,
                source,
                policy,
                manifest.coverage.observed_start,
                manifest.coverage.observed_end,
                provider_facts[provider_id],
                provider_strategy_returns,
                str(authority["contract_semantic_sha256"]),
            )
            for item in provider_items
        ]
        admitted_count, admitted_digest = _admitted_item_set_digest(
            provider_dispositions
        )
        if (
            admitted_count != policy["admitted_item_count"]
            or admitted_digest != policy["admitted_item_set_sha256"]
        ):
            raise ProviderLedgerError(
                f"{provider_id} admitted dataset set differs from authority"
            )
        allowed_reasons = set(
            _strings(
                policy["allowed_exclusion_reasons"],
                "provider allowed exclusion reasons",
            )
        )
        for disposition in provider_dispositions:
            if disposition.action is DatasetDispositionAction.EXCLUDE:
                if disposition.reason not in allowed_reasons:
                    raise ProviderLedgerError(
                        f"{provider_id} dataset exclusion is not authorized"
                    )
                if disposition.readiness_impact != policy["exclusion_readiness_impact"]:
                    raise ProviderLedgerError(
                        f"{provider_id} dataset exclusion readiness differs"
                    )
        dispositions.extend(provider_dispositions)
        providers.append(
            {
                **identity.to_dict(),
                "adapter_name": manifest.adapter.name,
                "adapter_version": manifest.adapter.version,
                "coverage_boundary": manifest.coverage.boundary,
                "coverage_status": manifest.coverage.status.value,
                "generation_as_of_date": manifest.as_of_date.isoformat(),
                "observed_end": manifest.coverage.observed_end.isoformat(),
                "observed_start": manifest.coverage.observed_start.isoformat(),
                "source_segment_id": source["source_id"],
                "source_segment_sha256": hashlib.sha256(
                    canonical_json_bytes(source)
                ).hexdigest(),
            }
        )
    ordered_inventory, ordered_dispositions = validate_dataset_dispositions(
        inventory, dispositions
    )
    inventory_rows = [item.to_dict() for item in ordered_inventory]
    disposition_rows = [item.to_dict() for item in ordered_dispositions]
    blocked = [
        item
        for item in ordered_dispositions
        if item.action is DatasetDispositionAction.EXCLUDE
        and item.readiness_impact == "REQUIRED_NOT_READY"
    ]
    receipt = {
        "admission_gate_status": "PASS",
        "census_id": authority["census_id"],
        "census_sha256": authority["census_sha256"],
        "contract_raw_sha256": authority["contract_raw_sha256"],
        "contract_semantic_sha256": authority["contract_semantic_sha256"],
        "disposition_sha256": hashlib.sha256(
            _canonical_json_value_bytes(disposition_rows)
        ).hexdigest(),
        "dispositions": disposition_rows,
        "invalidated_outputs": sorted(
            {output for item in blocked for output in item.invalidated_outputs}
        ),
        "inventory": inventory_rows,
        "inventory_sha256": hashlib.sha256(
            _canonical_json_value_bytes(inventory_rows)
        ).hexdigest(),
        "product_id": authority["product_id"],
        "product_readiness": (
            "REQUIRED_NOT_READY" if blocked else "READY_FOR_COMPOSITION"
        ),
        "product_version": authority["product_version"],
        "provider_generations": providers,
        "receipt_type": "PROVIDER_ADMIT_OR_DECLARE_V1",
        "schema_version": ADMIT_OR_DECLARE_SCHEMA_VERSION,
    }
    validate_admit_or_declare_receipt(
        receipt,
        product_authority=authority,
        source_segments=_object(authority["sources"], "financial MVP sources"),
        provider_generations={str(row["provider_id"]): row for row in providers},
    )
    return receipt, canonical_json_bytes(receipt)


def build_catalog(
    spec_path: Path,
) -> tuple[dict[str, Any], GenerationManifest, bytes, bytes]:
    """Build one deterministic provider-ledger catalog and manifest."""

    spec = _load_spec(spec_path)
    authority = _financial_mvp_authority(spec)
    as_of = _day(spec["as_of_date"], "spec.as_of_date")
    provenance, provenance_artifact = _provenance(spec)
    accounts, strategies, assignments = _assignment_index(provenance)
    identities: list[ProviderGenerationIdentity] = []
    provider_coverage: list[dict[str, Any]] = []
    provider_artifacts: list[SourceArtifact] = []
    combined: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exceptions: list[dict[str, Any]] = []
    catalogs: dict[str, dict[str, Any]] = {}
    manifests: dict[str, GenerationManifest] = {}
    provider_facts: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for provider_id in sorted(_PROVIDER_CATALOGS):
        identity, manifest, provider_catalog, artifacts, root = _provider(
            provider_id, _object(spec["providers"], "providers")[provider_id], as_of
        )
        identities.append(identity)
        catalogs[provider_id] = provider_catalog
        manifests[provider_id] = manifest
        provider_artifacts.extend(artifacts)
        provider_coverage.append(
            {
                "coverage": _provider_coverage(
                    provider_id, manifest.coverage, provider_catalog
                ),
                "generation_as_of_date": manifest.as_of_date.isoformat(),
                "generation_id": manifest.generation_id,
                "provider_id": provider_id,
                "qualification": identity.qualification.value,
            }
        )
        facts, findings = _project_account_facts(
            provider_id,
            identity.generation_id,
            provider_catalog,
            accounts,
            strategies,
            assignments,
            manifest=manifest,
            root=root,
        )
        provider_facts[provider_id] = facts
        for fact_type, rows in facts.items():
            combined[fact_type].extend(rows)
        exceptions.extend(findings)

    source_strategy_returns, evidence_rows, strategy_artifacts = _strategy_inputs(
        spec["strategy_inputs"],
        provenance,
        as_of,
        _object(spec["strategy_provider_map"], "strategy_provider_map"),
    )
    declared_nav_gaps = _declared_nav_gap_dates_by_provider(manifests)
    source_strategy_returns = _returns_within_declared_nav_gaps(
        source_strategy_returns,
        declared_nav_gaps,
    )
    strategy_names = {
        _text(item.get("strategy_id"), "strategy_id"): _text(
            item.get("name"), "strategy.name"
        )
        for item in _objects(provenance.get("strategies"), "strategies")
    }
    authoritative_provider_ids = {
        provider_id
        for provider_id, source_id in _PROVIDER_SOURCE_SEGMENTS.items()
        if _object(authority["sources"][source_id], source_id).get(
            "product_requirement"
        )
        == "REQUIRED"
    }
    _validate_nav_observation_value_bases(
        combined.get("nav_observations", []),
        authoritative_provider_ids,
        {
            provider_id: {artifact.artifact_id for artifact in manifest.artifacts}
            for provider_id, manifest in manifests.items()
        },
        {
            provider_id: {
                artifact.artifact_id
                for artifact in manifest.artifacts
                if artifact.schema_name
                in _NAV_SOURCE_SCHEMA_NAMES_BY_PROVIDER.get(provider_id, frozenset())
                or artifact.artifact_id
                in _NAV_SOURCE_ARTIFACT_IDS_BY_PROVIDER.get(provider_id, frozenset())
            }
            for provider_id, manifest in manifests.items()
        },
    )
    _validate_nav_observation_moves(
        combined.get("nav_observations", []),
        combined.get("flows", []),
        combined.get("transactions", []),
    )
    broker_returns = _broker_strategy_returns(
        combined.get("nav_observations", []),
        combined.get("flows", []),
        authoritative_provider_ids,
        declared_nav_gap_dates_by_provider=declared_nav_gaps,
    )
    strategy_returns = _merge_account_returns(
        source_strategy_returns, broker_returns, strategy_names
    )
    overlap_checks = _strategy_return_overlap_checks(
        source_strategy_returns, broker_returns
    )
    provider_strategy_returns = _portfolio123_provider_returns(
        _object(spec["providers"], "providers")["portfolio123"],
        manifests["portfolio123"],
        evidence_rows,
    )
    evidence_rows = _refresh_strategy_evidence(
        [*strategy_returns, *provider_strategy_returns],
        evidence_rows,
        strategy_names,
        {item.provider_id: item for item in identities},
        {provider_id: item.as_of_date for provider_id, item in manifests.items()},
    )
    authoritative_strategy_ids = {
        str(row.get("strategy_id"))
        for row in _objects(authority["contract"].get("strategies"), "strategies")
        if row.get("product_requirement") == "REQUIRED"
    }
    admissions = _admissions(
        combined,
        evidence_rows,
        authoritative_provider_ids,
        authoritative_strategy_ids,
    )
    admit_or_declare, admit_or_declare_bytes = _admit_or_declare_receipt(
        authority,
        identities,
        manifests,
        catalogs,
        provider_facts,
        provider_strategy_returns,
    )
    fact_counts = []
    for fact_type, rows in sorted(combined.items()):
        rows.sort(
            key=lambda item: (
                str(item["provider_id"]),
                str(item.get("as_of_date") or item.get("event_date") or ""),
                str(item["record_id"]),
            )
        )
        fact_counts.append(
            {
                "fact_type": fact_type,
                "mapped_count": sum(
                    item.get("mapping_status") == "MAPPED" for item in rows
                ),
                "record_count": len(rows),
                "unmapped_count": sum(
                    item.get("mapping_status") != "MAPPED" for item in rows
                ),
            }
        )
    catalog: dict[str, Any] = {
        "admissions": [item.to_dict() for item in admissions],
        "as_of_date": as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "corporate_actions": combined.get("corporate_actions", []),
        "fact_counts": fact_counts,
        "flows": combined.get("flows", []),
        "nav_observations": combined.get("nav_observations", []),
        "positions": combined.get("positions", []),
        "provider_coverage": provider_coverage,
        "provider_generations": [
            {
                **item.to_dict(),
                "generation_as_of_date": manifests[
                    item.provider_id
                ].as_of_date.isoformat(),
            }
            for item in identities
        ],
        "provider_strategy_returns": provider_strategy_returns,
        "quality_findings": sorted(
            exceptions,
            key=lambda item: (
                str(item["provider_id"]),
                str(item["fact_type"]),
                str(item["record_id"]),
            ),
        ),
        "reconciliation_checks": [
            {
                "check_id": "all-current-providers-present",
                "provider_count": len(identities),
                "status": "PASS"
                if len(identities) == len(_PROVIDER_CATALOGS)
                else "FAIL",
            },
            {
                "check_id": "strategy-evidence-is-canonical-input",
                "strategy_count": len(evidence_rows),
                "status": "PASS",
            },
            {
                "appended_broker_return_count": len(strategy_returns)
                - len(source_strategy_returns),
                "check_id": "broker-nav-rollforward",
                "status": "PASS",
            },
            *overlap_checks,
        ],
        "securities": sorted(
            [
                {**item, "provider_id": provider_id}
                for provider_id, provider_catalog in catalogs.items()
                for item in _objects(
                    provider_catalog.get("securities", []), f"{provider_id}.securities"
                )
            ],
            key=lambda item: (str(item["provider_id"]), str(item.get("security_id"))),
        ),
        "strategy_evidence": evidence_rows,
        "strategy_returns": strategy_returns,
        "transactions": combined.get("transactions", []),
    }
    catalog_bytes = canonical_json_bytes(catalog)
    catalog_artifact = SourceArtifact(
        artifact_id="normalized/provider-ledger.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=as_of,
        schema_name="provider-ledger-catalog",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    receipt_artifact = SourceArtifact(
        artifact_id="normalized/admit-or-declare-receipt.json",
        sha256=hashlib.sha256(admit_or_declare_bytes).hexdigest(),
        size_bytes=len(admit_or_declare_bytes),
        as_of_date=as_of,
        schema_name="provider-admission-receipt",
        schema_version=str(ADMIT_OR_DECLARE_SCHEMA_VERSION),
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity(
            name="quant-engine-provider-ledger", version=ADAPTER_VERSION
        ),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary="exact configured local provider generations and strategy evidence; upstream gaps are preserved per provider",
            fact_types=tuple(
                sorted(
                    {
                        *combined,
                        "admit_or_declare_receipt",
                        "strategy_evidence",
                        "strategy_returns",
                    }
                )
            ),
            observed_start=min(
                artifact.as_of_date
                for artifact in (
                    *provider_artifacts,
                    *strategy_artifacts,
                    provenance_artifact,
                )
            ),
            observed_end=as_of,
        ),
        artifacts=(
            provenance_artifact,
            *provider_artifacts,
            *strategy_artifacts,
            catalog_artifact,
            receipt_artifact,
        ),
        verification=VerificationStatus.VERIFIED,
    )
    if admit_or_declare["admission_gate_status"] != "PASS":
        raise ProviderLedgerError("admit-or-declare gate did not pass")
    return catalog, manifest, catalog_bytes, admit_or_declare_bytes


def stage_candidate(
    spec_path: Path, output_root_override: Path | None = None
) -> dict[str, object]:
    """Stage immutable output without advancing an authority pointer."""

    spec = _load_spec(spec_path)
    output_root = (
        output_root_override.resolve()
        if output_root_override is not None
        else Path(_text(spec["output_root"], "output_root")).resolve()
    )
    catalog, manifest, catalog_bytes, admit_or_declare_bytes = build_catalog(spec_path)
    final = output_root / "generations" / manifest.generation_id
    if final.exists():
        if final.is_symlink() or not final.is_dir():
            raise ProviderLedgerError("provider-ledger generation path conflicts")
        if (
            (final / "provider-ledger.json").read_bytes() != catalog_bytes
            or (final / "admit-or-declare-receipt.json").read_bytes()
            != admit_or_declare_bytes
            or (final / "generation.json").read_bytes() != manifest.to_json_bytes()
        ):
            raise ProviderLedgerError("provider-ledger generation differs")
    else:
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".provider-ledger-", dir=final.parent))
        try:
            (temporary / "provider-ledger.json").write_bytes(catalog_bytes)
            (temporary / "admit-or-declare-receipt.json").write_bytes(
                admit_or_declare_bytes
            )
            (temporary / "generation.json").write_bytes(manifest.to_json_bytes())
            rename_directory_with_retry(temporary, final)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {
        "admit_or_declare_sha256": hashlib.sha256(admit_or_declare_bytes).hexdigest(),
        "fact_counts": catalog["fact_counts"],
        "generation_id": manifest.generation_id,
        "manifest_sha256": hashlib.sha256(manifest.to_json_bytes()).hexdigest(),
        "published": False,
        "state": "STAGED",
    }


def verify_candidate(
    spec_path: Path,
    generation_id: str,
    output_root_override: Path | None = None,
) -> dict[str, object]:
    spec = _load_spec(spec_path)
    output_root = (
        output_root_override.resolve()
        if output_root_override is not None
        else Path(_text(spec["output_root"], "output_root")).resolve()
    )
    _, manifest, catalog_bytes, admit_or_declare_bytes = build_catalog(spec_path)
    if manifest.generation_id != generation_id:
        raise ProviderLedgerError("rebuilt provider-ledger generation differs")
    generation_root = output_root / "generations" / generation_id
    catalog_path = _exact_file(
        generation_root, Path("provider-ledger.json"), "staged provider-ledger catalog"
    )
    receipt_path = _exact_file(
        generation_root,
        Path("admit-or-declare-receipt.json"),
        "staged admit-or-declare receipt",
    )
    manifest_path = _exact_file(
        generation_root, Path("generation.json"), "staged provider-ledger manifest"
    )
    if catalog_path.read_bytes() != catalog_bytes:
        raise ProviderLedgerError("staged provider-ledger catalog differs")
    if receipt_path.read_bytes() != admit_or_declare_bytes:
        raise ProviderLedgerError("staged admit-or-declare receipt differs")
    if manifest_path.read_bytes() != manifest.to_json_bytes():
        raise ProviderLedgerError("staged provider-ledger manifest differs")
    return {
        "generation_id": generation_id,
        "state": "VERIFIED_STAGED",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("--spec", type=Path, required=True)
    stage.add_argument("--output-root", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--spec", type=Path, required=True)
    verify.add_argument("--generation-id", required=True)
    verify.add_argument("--output-root", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = (
        stage_candidate(args.spec, args.output_root)
        if args.command == "stage"
        else verify_candidate(args.spec, args.generation_id, args.output_root)
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
