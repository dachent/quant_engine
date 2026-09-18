"""Build one deterministic provider-to-ledger admission candidate.

The tool performs no network or credential operation. It reads only exact local
provider generations and hash-bound portfolio evidence named by a private spec.
Provider observations are retained separately from authority; only account-live
facts with an accepted account and effective strategy assignment can affect the
canonical portfolio projection.
"""

from __future__ import annotations

import argparse
import base64
from collections import defaultdict
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from functools import wraps
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Callable, Final, Mapping, Sequence
from urllib.parse import urlsplit

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
ADAPTER_VERSION: Final = "5"
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
        "capital_anchors",
        "corporate_actions",
        "flows",
        "nav_observations",
        "positions",
        "transactions",
    ),
    "matrixport": ("capital_anchors", "nav_observations"),
    "portfolio123": (),
    "quantbase": ("capital_anchors", "flows", "nav_observations"),
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
    "CAPITAL_ANCHOR": "capital_anchors",
    "POSITION": "positions",
    "TRANSACTION": "transactions",
    "EXTERNAL_FLOW": "flows",
    "CORPORATE_ACTION": "corporate_actions",
    "PROVIDER_LIVE_RETURN": "provider_strategy_returns",
}
_TARGET_ADMISSION_FACT: Final = {
    "corporate_actions": "CORPORATE_ACTION",
    "capital_anchors": "CAPITAL_ANCHOR",
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
    "account_activity": "capital_anchors",
    "subscription_details": "capital_anchors",
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
_MATRIXPORT_ROBUXIO_PRODUCT_ID: Final = "7361599360326447104"
_MATRIXPORT_ROBUXIO_ORDER_ID: Final = "7402822649690198016"
_QUANTBASE_FUND_ID: Final = "107"
_SCHWAB_FLOW_KINDS: Final = frozenset(
    {
        "Bank Transfer",
        "Funds Received",
        "MoneyLink Deposit",
        "MoneyLink Transfer",
        "MoneyLink Adj",
        "Security Transfer",
        "Wire Sent",
    }
)
# Pins the retained, independently audited Schwab Journal records to the
# accepted Schwab refresh generation 4e47b7b8 (dataSchwab CURRENT) as of
# 2026-09-04, whose per-account WCH transaction captures re-assigned two
# record_ids and renamed both source artifacts.
_SCHWAB_PINNED_JOURNAL_EXTERNAL_FLOWS: Final = {
    (
        "account-38b653b91ff43a10",
        "txn-84ed102899eaaa7ba1d2f0d4",
        "2025-12-29",
        Decimal("7000"),
    ): {
        "source_artifact_id": "wch/transactions/account-38b653b91ff43a10.json",
        "source_artifact_sha256": (
            "521cadb20162bba19a39064e2ed165425493ac37df1f35638653bcb2ebfa6514"
        ),
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": (
            "b9b1dbee9c9204408c81a823dde6d734d50104c6289aeb4da0b460a5e0f7fe95"
        ),
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
    (
        "account-38b653b91ff43a10",
        "txn-855f069db9ce99988c6707ea",
        "2026-01-02",
        Decimal("7500"),
    ): {
        "source_artifact_id": "wch/transactions/account-38b653b91ff43a10.json",
        "source_artifact_sha256": (
            "521cadb20162bba19a39064e2ed165425493ac37df1f35638653bcb2ebfa6514"
        ),
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": (
            "1d7acaaa5abb230c0cb48200c066e49fa0b554c2f2a4b3c321fcf0342fc85957"
        ),
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
    (
        "account-a13937e26b8ae46d",
        "txn-249d9f96964cbf7860830e82",
        "2025-12-29",
        Decimal("7000"),
    ): {
        "source_artifact_id": "wch/transactions/account-a13937e26b8ae46d.json",
        "source_artifact_sha256": "46ed86a97801fa9fc8905b7ccd2857b65ebed8f8915d6ba1fd81bad88a565688",
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": "3a2ef670763f2bf9938ee37386b05f376a46a2d3233d9541b49bfad56db5c747",
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
    (
        "account-a13937e26b8ae46d",
        "txn-2f56f4cba6c5742b593c4ef2",
        "2026-01-02",
        Decimal("7500"),
    ): {
        "source_artifact_id": "wch/transactions/account-a13937e26b8ae46d.json",
        "source_artifact_sha256": "46ed86a97801fa9fc8905b7ccd2857b65ebed8f8915d6ba1fd81bad88a565688",
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": "d0b1437e4bcab56cc53f76f88206c36dc2c7b21301303705c695606fb3ac9f32",
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
    (
        "account-1825d6880d891d57",
        "txn-dcaf6ac1f18e29cc54a3016c",
        "2025-12-29",
        Decimal("-7000"),
    ): {
        "source_artifact_id": "wch/transactions/account-1825d6880d891d57.json",
        "source_artifact_sha256": "9710d06c33985e76b62f3937443e6e51647fb695586d6b4c0a1d57b79e9ec96e",
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": "b9b1dbee9c9204408c81a823dde6d734d50104c6289aeb4da0b460a5e0f7fe95",
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
    (
        "account-1825d6880d891d57",
        "txn-1270308650488c57b650afdc",
        "2025-12-29",
        Decimal("-7000"),
    ): {
        "source_artifact_id": "wch/transactions/account-1825d6880d891d57.json",
        "source_artifact_sha256": "9710d06c33985e76b62f3937443e6e51647fb695586d6b4c0a1d57b79e9ec96e",
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": "3a2ef670763f2bf9938ee37386b05f376a46a2d3233d9541b49bfad56db5c747",
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
    (
        "account-1825d6880d891d57",
        "txn-16dd3f367043beadcabd3387",
        "2026-01-02",
        Decimal("-7500"),
    ): {
        "source_artifact_id": "wch/transactions/account-1825d6880d891d57.json",
        "source_artifact_sha256": "9710d06c33985e76b62f3937443e6e51647fb695586d6b4c0a1d57b79e9ec96e",
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": "1d7acaaa5abb230c0cb48200c066e49fa0b554c2f2a4b3c321fcf0342fc85957",
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
    (
        "account-1825d6880d891d57",
        "txn-8e9ac98ba8accadd5544238b",
        "2026-01-02",
        Decimal("-7500"),
    ): {
        "source_artifact_id": "wch/transactions/account-1825d6880d891d57.json",
        "source_artifact_sha256": "9710d06c33985e76b62f3937443e6e51647fb695586d6b4c0a1d57b79e9ec96e",
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": "d0b1437e4bcab56cc53f76f88206c36dc2c7b21301303705c695606fb3ac9f32",
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
    (
        "account-61092059d066f056",
        "txn-3361828c64f99b1e768e2575",
        "2026-01-02",
        Decimal("50000"),
    ): {
        "source_artifact_id": "wch/transactions/account-61092059d066f056.json",
        "source_artifact_sha256": "5349b613f8863df1931f13cc8cc787c5e766c0bd0e749f21c5a5ae00e90d0425",
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": "6a69ba3b1c0054bd7c77fd972ad21af4beca0871600a2c62cd7e3d49e57b370a",
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
    (
        "account-702c489650e59956",
        "txn-1699da38ec4afacd94bd2876",
        "2026-01-02",
        Decimal("-50000"),
    ): {
        "source_artifact_id": "wch/transactions/account-702c489650e59956.json",
        "source_artifact_sha256": "69856742fdf963136a326fc61ca4897e0a5e9a1d99c7e1db4231625c9ab9ee2a",
        "flow_type": "ACCOUNT_TRANSFER",
        "mapping_basis": "PINNED_PAIRED_PROVIDER_JOURNAL",
        "provider_activity_id_sha256": "6a69ba3b1c0054bd7c77fd972ad21af4beca0871600a2c62cd7e3d49e57b370a",
        "source_classification": "PAIRED_INTERNAL_JOURNAL",
    },
}
_DATE_FIELDS: Final = (
    "as_of_date",
    "event_date",
    "effective_date",
    "nav_recognition_date",
)
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
    "matrixport": frozenset(
        {"csv/daily_holdings.csv", "csv/daily_portfolio_totals.csv"}
    ),
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


def _strict(value: Mapping[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise ProviderLedgerError(f"{name} keys differ")


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
    io_path = (
        Path(f"\\\\?\\{path}")
        if sys.platform == "win32" and not str(path).startswith("\\\\?\\")
        else path
    )
    if io_path.is_symlink() or not io_path.is_file():
        raise ProviderLedgerError(f"{name} must be a regular file")
    return io_path


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


def _verified_evidence_bytes(
    path: Path, expected: object, name: str
) -> tuple[bytes, str, int]:
    """Hash-verify one finality input before adapter-specific interpretation."""

    if path.is_symlink() or not path.is_file():
        raise ProviderLedgerError(f"{name} must be a regular file")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ProviderLedgerError(f"{name} is not readable") from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != _text(expected, f"{name}.sha256"):
        raise ProviderLedgerError(f"{name} hash differs")
    return content, digest, len(content)


def _absolute_path(value: object, name: str) -> Path:
    """Resolve one explicitly absolute, non-linked local source path."""

    path = Path(_text(value, name))
    if not path.is_absolute():
        raise ProviderLedgerError(f"{name} must be absolute")
    if path.is_symlink():
        raise ProviderLedgerError(f"{name} must not be linked")
    return path.resolve()


def _absolute_regular_path(value: object, name: str) -> Path:
    """Resolve one explicitly absolute, non-linked local evidence file."""

    resolved = _absolute_path(value, name)
    if resolved.is_symlink() or not resolved.is_file():
        raise ProviderLedgerError(f"{name} must be a regular file")
    return resolved


def _utc_timestamp(value: object, name: str) -> str:
    """Accept a timezone-aware UTC timestamp without assigning a timezone."""

    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderLedgerError(f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProviderLedgerError(f"{name} must be timezone-aware UTC")
    return text


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


def _matrixport_source_backed_usd_dates(
    manifest: GenerationManifest,
    run_root: Path,
) -> set[date]:
    """Return dates whose active holdings have source-backed USD values.

    ``strict_complete`` is a completeness flag produced by the Matrixport
    extractor.  It does not establish that a non-USD holding was converted by
    a provider-reported value or rate.  In particular, the retained Robuxio
    generation labels its USDT rows ``assumed_stablecoin_parity``.  Those rows
    remain native-currency source facts and cannot be promoted to USD NAV.
    """
    holdings_artifact_id = "csv/daily_holdings.csv"
    holdings_artifact = _find_manifest_artifact(
        manifest, holdings_artifact_id, "matrixport daily holdings"
    )
    holdings = _verified_csv_rows(
        _exact_file(run_root, Path(holdings_artifact_id), "matrixport daily holdings"),
        holdings_artifact.sha256,
        {
            "currency",
            "native_value",
            "share",
            "utc_date",
            "valuation_source",
            "value_usd_api_rate",
            "value_usd_api_reported",
            "value_usd_strict_component",
        },
        "matrixport daily holdings",
    )
    issues_artifact_id = "csv/valuation_quality_issues.csv"
    issues_artifact = _find_manifest_artifact(
        manifest, issues_artifact_id, "matrixport valuation quality issues"
    )
    issues = _verified_csv_rows(
        _exact_file(
            run_root,
            Path(issues_artifact_id),
            "matrixport valuation quality issues",
        ),
        issues_artifact.sha256,
        {"reason", "source_index", "source_table"},
        "matrixport valuation quality issues",
    )
    conflict_dates = {
        _day(row["source_index"], "matrixport valuation issue source_index")
        for row in issues
        if row.get("source_table") == "daily_holdings"
        and str(row.get("reason") or "").startswith("parity_fx_rate_conflict:")
    }
    by_day: dict[date, list[dict[str, str]]] = defaultdict(list)
    for row in holdings:
        day = _day(row.get("utc_date"), "matrixport daily holdings utc_date")
        share = _observed_decimal(row.get("share"), "matrixport daily holdings share")
        if share is None:
            raise ProviderLedgerError(
                "MATRIXPORT_ACCOUNT_COMPONENT_INVALID: active share is absent"
            )
        if share != 0:
            by_day[day].append(row)

    qualified: set[date] = set()
    for day, rows in by_day.items():
        if day in conflict_dates:
            continue
        supported = True
        for row in rows:
            valuation_source = str(row.get("valuation_source") or "")
            if valuation_source.startswith(("assumed_", "missing_")):
                supported = False
                break
            native_value = _observed_decimal(
                row.get("native_value"), "matrixport native holding value"
            )
            strict_value = _observed_decimal(
                row.get("value_usd_strict_component"),
                "matrixport strict USD holding value",
            )
            if native_value is None or strict_value is None:
                supported = False
                break
            currency = _text(row.get("currency"), "matrixport holding currency")
            if currency == "USD" and native_value == strict_value:
                continue
            reported_value = _observed_decimal(
                row.get("value_usd_api_reported"),
                "matrixport API-reported USD holding value",
            )
            if reported_value is not None and reported_value == strict_value:
                continue
            reported_rate = _observed_decimal(
                row.get("value_usd_api_rate"),
                "matrixport API-reported USD rate",
            )
            if (
                reported_rate is not None
                and reported_rate > 0
                and native_value * reported_rate == strict_value
            ):
                continue
            supported = False
            break
        if supported:
            qualified.add(day)
    return qualified


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


def _daily_fx_evidence(
    currency: str,
    day: date,
    fx_rates: tuple[
        str,
        Mapping[tuple[str, date], Decimal],
        tuple[SourceArtifact, SourceArtifact],
    ],
) -> dict[str, Any]:
    generation_id, rates, _artifacts = fx_rates
    rate = rates.get((currency, day))
    if rate is None or rate <= 0:
        raise ProviderLedgerError(
            f"portfolio FX lacks {currency}/USD on {day.isoformat()}"
        )
    return {
        "base_currency": "USD",
        "currency": currency,
        "date": day.isoformat(),
        "field": "Close",
        "generation_id": generation_id,
        "provider": "yahoo",
        "rate": format(rate, "f"),
        "source_artifact_id": "rates.csv",
        "timing": "UTC_DAILY_CLOSE",
    }


def _excluded_source_row(
    findings: list[dict[str, Any]] | None,
    provider_id: str,
    fact_type: str,
    artifact_id: str,
    row: Mapping[str, Any],
    observed: date,
    cutoff: date,
    *,
    original_source_dates: Mapping[str, str] | None = None,
) -> None:
    """Retain the identity of a native row excluded before derivation."""
    if findings is None:
        return
    row_hash = hashlib.sha256(canonical_json_bytes(dict(row))).hexdigest()
    source_id = str(
        row.get("record_id")
        or row.get("transaction_id")
        or row.get("order_id")
        or row.get("id")
        or row.get("time")
        or row_hash
    )
    finding: dict[str, Any] = {
        "financial_cutoff": cutoff.isoformat(),
        "fact_type": fact_type,
        "original_record_id": source_id,
        "original_source_date": observed.isoformat(),
        "provider_id": provider_id,
        "record_id": f"{provider_id}:{source_id}",
        "source_artifact_id": artifact_id,
        "source_record_id": source_id,
        "source_row_sha256": row_hash,
        "status": "OUTSIDE_FINANCIAL_CUTOFF",
    }
    if original_source_dates is not None:
        finding["original_source_dates"] = dict(original_source_dates)
    if finding not in findings:
        findings.append(finding)


def _matrixport_daily_fx_totals(
    manifest: GenerationManifest,
    run_root: Path,
    fx_rates: tuple[
        str,
        Mapping[tuple[str, date], Decimal],
        tuple[SourceArtifact, SourceArtifact],
    ],
    financial_cutoff: date | None = None,
    cutoff_findings: list[dict[str, Any]] | None = None,
) -> dict[date, tuple[Decimal, list[dict[str, Any]]]]:
    """Convert every active native holding using its exact daily Close."""

    artifact_id = "csv/daily_holdings.csv"
    artifact = _find_manifest_artifact(
        manifest, artifact_id, "matrixport daily holdings"
    )
    rows = _verified_csv_rows(
        _exact_file(run_root, Path(artifact_id), "matrixport daily holdings"),
        artifact.sha256,
        {"currency", "native_value", "share", "utc_date"},
        "matrixport daily holdings",
    )
    totals: dict[date, Decimal] = defaultdict(Decimal)
    evidence: dict[date, dict[str, dict[str, Any]]] = defaultdict(dict)
    incomplete_days: set[date] = set()
    with localcontext() as context:
        context.prec = 80
        for row in rows:
            day = _day(row.get("utc_date"), "matrixport daily holdings utc_date")
            if financial_cutoff is not None and day > financial_cutoff:
                _excluded_source_row(
                    cutoff_findings,
                    "matrixport",
                    "nav_observations",
                    artifact_id,
                    row,
                    day,
                    financial_cutoff,
                )
                continue
            share = _observed_decimal(
                row.get("share"), "matrixport daily holdings share"
            )
            if share is None:
                raise ProviderLedgerError(
                    "MATRIXPORT_ACCOUNT_COMPONENT_INVALID: active share is absent"
                )
            if share == 0:
                continue
            native = _observed_decimal(
                row.get("native_value"), "matrixport native holding value"
            )
            if native is None:
                incomplete_days.add(day)
                continue
            currency = _text(row.get("currency"), "matrixport holding currency")
            if currency == "USD":
                totals[day] += native
                continue
            fx = _daily_fx_evidence(currency, day, fx_rates)
            totals[day] += native * Decimal(str(fx["rate"]))
            evidence[day][currency] = fx
    for day in incomplete_days:
        totals.pop(day, None)
        evidence.pop(day, None)
    if not totals:
        raise ProviderLedgerError(
            "MATRIXPORT_ACCOUNT_COMPONENTS_UNAVAILABLE: no active daily holdings exist"
        )
    return {
        day: (
            total,
            [evidence[day][currency] for currency in sorted(evidence[day])],
        )
        for day, total in totals.items()
    }


def _matrixport_daily_native_totals(
    manifest: GenerationManifest,
    run_root: Path,
    financial_cutoff: date | None = None,
) -> dict[date, dict[str, Decimal]]:
    """Total each day's active native holdings in their own currency.

    The owner-selected FX total proves the account's USD value.  Proving that
    one subscription funded the whole account has to happen in the currency the
    subscription was denominated in, where the comparison is exact and no rate
    stands between the two figures.
    """

    artifact_id = "csv/daily_holdings.csv"
    artifact = _find_manifest_artifact(
        manifest, artifact_id, "matrixport daily holdings"
    )
    rows = _verified_csv_rows(
        _exact_file(run_root, Path(artifact_id), "matrixport daily holdings"),
        artifact.sha256,
        {"currency", "native_value", "share", "utc_date"},
        "matrixport daily holdings",
    )
    totals: dict[date, dict[str, Decimal]] = defaultdict(dict)
    incomplete_days: set[date] = set()
    with localcontext() as context:
        context.prec = 80
        for row in rows:
            day = _day(row.get("utc_date"), "matrixport daily holdings utc_date")
            if financial_cutoff is not None and day > financial_cutoff:
                continue
            share = _observed_decimal(
                row.get("share"), "matrixport daily holdings share"
            )
            if share is None:
                raise ProviderLedgerError(
                    "MATRIXPORT_ACCOUNT_COMPONENT_INVALID: active share is absent"
                )
            if share == 0:
                continue
            native = _observed_decimal(
                row.get("native_value"), "matrixport native holding value"
            )
            if native is None:
                incomplete_days.add(day)
                continue
            currency = _text(row.get("currency"), "matrixport holding currency")
            totals[day][currency] = totals[day].get(currency, Decimal(0)) + native
    for day in incomplete_days:
        totals.pop(day, None)
    return dict(totals)


def _matrixport_flows(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    fx_rates: tuple[
        str,
        Mapping[tuple[str, date], Decimal],
        tuple[SourceArtifact, SourceArtifact],
    ],
    financial_cutoff: date | None = None,
    cutoff_findings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project confirmed wallet deposits and withdrawals in their native units."""

    account_id = _CSV_FACT_ACCOUNT["matrixport"]
    if account_id not in accounts:
        raise ProviderLedgerError("matrixport wallet account is unknown")
    source_run_id = _run_id(catalog.get("source_run_id"), "matrixport.source_run_id")
    run_root = root / "output" / source_run_id
    datasets = (
        ("csv/wallet_deposits.csv", Decimal("1")),
        ("csv/wallet_withdrawals.csv", Decimal("-1")),
    )
    projected: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for artifact_id, sign in datasets:
        artifacts = [
            artifact
            for artifact in manifest.artifacts
            if artifact.artifact_id == artifact_id
        ]
        if not artifacts:
            continue
        rows = _verified_csv_rows(
            _exact_file(run_root, Path(artifact_id), f"matrixport {artifact_id}"),
            artifacts[0].sha256,
            {"amount", "created_at_utc", "currency", "state", "transaction_id"},
            f"matrixport {artifact_id}",
        )
        for row in rows:
            if str(row.get("state") or "").lower() not in {
                "completed",
                "confirmed",
                "success",
            }:
                continue
            source_id = _text(row.get("transaction_id"), "matrixport transaction_id")
            row_sha256 = hashlib.sha256(canonical_json_bytes(dict(row))).hexdigest()
            duplicate = seen.get(source_id)
            if duplicate is not None:
                if duplicate != row_sha256:
                    raise ProviderLedgerError(
                        "conflicting duplicate Matrixport wallet transaction"
                    )
                continue
            seen[source_id] = row_sha256
            amount = _observed_decimal(row.get("amount"), "matrixport wallet amount")
            if amount is None or amount <= 0:
                raise ProviderLedgerError("matrixport wallet amount is invalid")
            try:
                timestamp = datetime.fromisoformat(
                    _text(
                        row.get("created_at_utc"),
                        "matrixport wallet created_at_utc",
                    ).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ProviderLedgerError(
                    "matrixport wallet timestamp is invalid"
                ) from exc
            if (
                timestamp.tzinfo is None
                or timestamp.utcoffset() is None
                or timestamp.utcoffset() != timedelta(0)
            ):
                raise ProviderLedgerError("matrixport wallet timestamp is not UTC")
            if financial_cutoff is not None and timestamp.date() > financial_cutoff:
                _excluded_source_row(
                    cutoff_findings,
                    "matrixport",
                    "flows",
                    artifact_id,
                    row,
                    timestamp.date(),
                    financial_cutoff,
                )
                continue
            currency = _text(row.get("currency"), "matrixport wallet currency")
            signed = amount * sign
            projected.append(
                {
                    "account_id": account_id,
                    "amount": {
                        "currency": currency,
                        "status": "AVAILABLE",
                        "value": format(signed.normalize(), "f"),
                    },
                    "capital_disposition": "EXTERNAL_CAPITAL",
                    "event_date": timestamp.date().isoformat(),
                    "event_time_fx": _daily_fx_evidence(
                        currency, timestamp.date(), fx_rates
                    ),
                    "event_timestamp": timestamp.isoformat(),
                    "evidence_layer": "ACCOUNT_LIVE",
                    "flow_type": "DEPOSIT_OR_WITHDRAWAL",
                    "generation_id": generation_id,
                    "mapping_basis": "CONFIRMED_MATRIXPORT_WALLET_HISTORY_NATIVE",
                    "mapping_status": "UNMAPPED",
                    "provider_account_scope": "ACCOUNT_ONLY",
                    "provider_id": "matrixport",
                    "record_id": f"matrixport-flow:{source_id}",
                    "source_artifact_id": artifact_id,
                    "source_record_id": source_id,
                    "source_row_sha256": row_sha256,
                    "strategy_id": None,
                }
            )
    projected.sort(key=lambda row: (str(row["event_timestamp"]), str(row["record_id"])))
    return projected


def _matrixport_nav_observations(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
    fx_rates: tuple[
        str,
        Mapping[tuple[str, date], Decimal],
        tuple[SourceArtifact, SourceArtifact],
    ]
    | None = None,
    financial_cutoff: date | None = None,
    cutoff_findings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Admit Matrixport daily portfolio totals as account NAV observations."""
    account_id = _CSV_FACT_ACCOUNT["matrixport"]
    source_run_id = _run_id(catalog.get("source_run_id"), "matrixport.source_run_id")
    run_root = root / "output" / source_run_id
    if fx_rates is not None:
        daily_totals = _matrixport_daily_fx_totals(
            manifest, run_root, fx_rates, financial_cutoff, cutoff_findings
        )
        snapshot_date = _day(
            catalog.get("observed_boundary_date"),
            "matrixport observed_boundary_date",
        )
        expected_snapshot = (
            min(snapshot_date, financial_cutoff)
            if financial_cutoff is not None
            else snapshot_date
        )
        if max(daily_totals) != expected_snapshot:
            raise ProviderLedgerError(
                "MATRIXPORT_ACCOUNT_SNAPSHOT_DATE_MISMATCH: latest native holding "
                "is not aligned with the declared boundary"
            )
        fx_observations: list[dict[str, Any]] = []
        for day, (daily_value, valuation_fx) in sorted(daily_totals.items()):
            if daily_value <= 0:
                continue
            strategy_id, mapping_status = _mapped_strategy_for_account(
                account_id, day, accounts, strategies, assignments
            )
            source_id = day.isoformat()
            wrapped = {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": format(daily_value.normalize(), "f"),
            }
            fx_observations.append(
                {
                    "account_id": account_id,
                    "as_of_date": source_id,
                    "component_source_artifact_id": "csv/daily_holdings.csv",
                    "evidence_layer": "ACCOUNT_LIVE",
                    "generation_id": generation_id,
                    "mapping_status": mapping_status,
                    "market_value": dict(wrapped),
                    "nav": dict(wrapped),
                    "provider_id": "matrixport",
                    "record_id": f"matrixport:{source_id}",
                    "source_artifact_id": "csv/daily_holdings.csv",
                    "source_record_id": source_id,
                    "strategy_id": strategy_id,
                    "valuation_fx": valuation_fx,
                    "value_basis": "ACCOUNT_TOTAL",
                }
            )
        return fx_observations
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
    source_backed_dates = _matrixport_source_backed_usd_dates(manifest, run_root)
    observations: list[dict[str, Any]] = []
    for row in rows:
        day = _day(row["utc_date"], "matrixport portfolio utc_date")
        if financial_cutoff is not None and day > financial_cutoff:
            _excluded_source_row(
                cutoff_findings,
                "matrixport",
                "nav_observations",
                artifact_id,
                row,
                day,
                financial_cutoff,
            )
            continue
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
        if day not in source_backed_dates:
            continue
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
        expected_snapshot = (
            min(snapshot_date, financial_cutoff)
            if financial_cutoff is not None
            else snapshot_date
        )
        if latest["as_of_date"] != expected_snapshot.isoformat():
            raise ProviderLedgerError(
                "MATRIXPORT_ACCOUNT_SNAPSHOT_DATE_MISMATCH: latest strict "
                "account total is not aligned with the daily-holdings boundary"
            )
        component_total = _matrixport_daily_component_total(
            manifest, run_root, expected_snapshot
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


def _matrixport_capital_anchors(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
    fx_rates: tuple[
        str,
        Mapping[tuple[str, date], Decimal],
        tuple[SourceArtifact, SourceArtifact],
    ]
    | None = None,
    financial_cutoff: date | None = None,
    cutoff_findings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Admit exact strategy subscriptions without allocating wallet deposits.

    A wallet deposit proves only account-level funding.  It does not establish
    the strategy's funded amount across currencies, fees, or retained cash.
    The subscription record, by contrast, names the product and its exact
    source-declared subscription amount.
    """
    account_id = _CSV_FACT_ACCOUNT["matrixport"]
    source_run_id = _run_id(catalog.get("source_run_id"), "matrixport.source_run_id")
    run_root = root / "output" / source_run_id
    artifact_id = "csv/subscription_details.csv"
    artifact = _find_manifest_artifact(
        manifest, artifact_id, "matrixport subscription details"
    )
    rows = _verified_csv_rows(
        _exact_file(run_root, Path(artifact_id), "matrixport subscription details"),
        artifact.sha256,
        {
            "actual_subscription_amount",
            "confirm_nav_time_utc",
            "order_id",
            "product_id",
            "subscription_amount",
            "subscription_currency",
            "subscription_time_utc",
        },
        "matrixport subscription details",
    )
    matching_rows = [
        row
        for row in rows
        if row.get("product_id") == _MATRIXPORT_ROBUXIO_PRODUCT_ID
        and row.get("order_id") == _MATRIXPORT_ROBUXIO_ORDER_ID
    ]
    if len(rows) != 1 or len(matching_rows) != 1:
        raise ProviderLedgerError("MATRIXPORT_ROBUXIO_SUBSCRIPTION_IDENTITY_DIFFERS")
    rows = matching_rows
    nav_artifact_id = "csv/daily_portfolio_totals.csv"
    nav_artifact = _find_manifest_artifact(
        manifest, nav_artifact_id, "matrixport daily portfolio totals"
    )
    nav_rows = _verified_csv_rows(
        _exact_file(
            run_root, Path(nav_artifact_id), "matrixport daily portfolio totals"
        ),
        nav_artifact.sha256,
        {
            "portfolio_value_usd_observed",
            "portfolio_value_usd_strict",
            "strict_complete",
            "utc_date",
        },
        "matrixport daily portfolio totals",
    )
    # Every retained Robuxio holding is USDT labelled
    # ``assumed_stablecoin_parity``, so the provider-reported USD route
    # qualifies no day at all and the one subscription was silently skipped.
    # The NAV series does not depend on that route: it converts each native
    # holding by the owner-selected daily close.  The anchor now rests on the
    # same evidence.
    fx_daily_totals: dict[date, tuple[Decimal, list[dict[str, Any]]]] | None = None
    native_totals: dict[date, dict[str, Decimal]] = {}
    source_backed_dates: set[date] = set()
    if fx_rates is None:
        source_backed_dates = _matrixport_source_backed_usd_dates(manifest, run_root)
    else:
        fx_daily_totals = _matrixport_daily_fx_totals(
            manifest, run_root, fx_rates, financial_cutoff, cutoff_findings
        )
        native_totals = _matrixport_daily_native_totals(
            manifest, run_root, financial_cutoff
        )

    def timestamp_day(value: object, name: str) -> date:
        try:
            return datetime.fromisoformat(
                _text(value, name).replace("Z", "+00:00")
            ).date()
        except ValueError as exc:
            raise ProviderLedgerError(f"{name} is not an ISO timestamp") from exc

    nav_rows_by_day: dict[date, list[dict[str, str]]] = defaultdict(list)
    for row in nav_rows:
        nav_rows_by_day[
            _day(row.get("utc_date"), "matrixport portfolio utc_date")
        ].append(row)

    anchors: list[dict[str, Any]] = []
    for row in sorted(
        rows, key=lambda item: (item["subscription_time_utc"], item["order_id"])
    ):
        amount = _observed_decimal(
            row.get("subscription_amount"), "matrixport subscription amount"
        )
        actual_amount = _observed_decimal(
            row.get("actual_subscription_amount"),
            "matrixport actual subscription amount",
        )
        if (
            amount is None
            or amount <= 0
            or actual_amount is None
            or actual_amount != amount
        ):
            raise ProviderLedgerError(
                "matrixport subscription amount is absent, non-positive, or inconsistent"
            )
        event_day = timestamp_day(
            row["subscription_time_utc"], "matrixport subscription time"
        )
        recognition_day = timestamp_day(
            row["confirm_nav_time_utc"], "matrixport subscription confirmation time"
        )
        if financial_cutoff is not None and (
            event_day > financial_cutoff or recognition_day > financial_cutoff
        ):
            _excluded_source_row(
                cutoff_findings,
                "matrixport",
                "capital_anchors",
                artifact_id,
                row,
                event_day,
                financial_cutoff,
            )
            continue
        qualified = (
            recognition_day in source_backed_dates
            if fx_daily_totals is None
            else recognition_day in fx_daily_totals
        )
        if not qualified:
            continue
        if recognition_day < event_day:
            raise ProviderLedgerError(
                "matrixport subscription recognition predates subscription"
            )
        # The subscription's economic date remains evidence of when capital
        # was committed.  Its funded epoch starts only when that capital is
        # recognized in the account NAV.
        strategy_id, mapping_status = _mapped_strategy_for_account(
            account_id, recognition_day, accounts, strategies, assignments
        )
        source_record_id = _text(
            row.get("order_id"), "matrixport subscription order_id"
        )
        opening = not anchors
        anchor: dict[str, Any] = {
            "account_id": account_id,
            "amount": {
                "currency": _text(
                    row.get("subscription_currency"),
                    "matrixport subscription currency",
                ),
                "status": "AVAILABLE",
                "value": format(amount.normalize(), "f"),
            },
            "capital_type": (
                "OPENING_CAPITAL_ANCHOR" if opening else "STRATEGY_CAPITAL_ALLOCATION"
            ),
            "event_date": event_day.isoformat(),
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_id": generation_id,
            "mapping_basis": "EXACT_STRATEGY_SUBSCRIPTION",
            "mapping_status": mapping_status,
            "nav_recognition_date": recognition_day.isoformat(),
            "provider_id": "matrixport",
            "record_id": f"matrixport-capital-anchor:{source_record_id}",
            "source_artifact_id": artifact_id,
            "source_product_id": _text(
                row.get("product_id"), "matrixport subscription product_id"
            ),
            "source_record_id": source_record_id,
            "strategy_id": strategy_id,
        }
        if opening and fx_daily_totals is not None:
            subscription_currency = _text(
                row.get("subscription_currency"),
                "matrixport subscription currency",
            )
            recognition_holdings = native_totals.get(recognition_day, {})
            if set(recognition_holdings) != {subscription_currency}:
                raise ProviderLedgerError(
                    "MATRIXPORT_OPENING_NAV_UNAVAILABLE: the recognition day's "
                    "active holdings are not the subscription currency alone"
                )
            if recognition_holdings[subscription_currency].quantize(
                Decimal("0.01")
            ) != amount.quantize(Decimal("0.01")):
                raise ProviderLedgerError(
                    "MATRIXPORT_OPENING_NAV_SUBSCRIPTION_MISMATCH"
                )
            opening_value, valuation_fx = fx_daily_totals[recognition_day]
            if opening_value <= 0:
                raise ProviderLedgerError(
                    "MATRIXPORT_OPENING_NAV_UNAVAILABLE: the owner-selected "
                    "conversion is non-positive"
                )
            anchor["opening_nav"] = {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": format(opening_value.normalize(), "f"),
            }
            anchor["nav_source_artifact_id"] = "csv/daily_holdings.csv"
            anchor["valuation_fx"] = valuation_fx
        elif opening:
            matched_nav_rows = nav_rows_by_day.get(recognition_day, [])
            if len(matched_nav_rows) != 1:
                raise ProviderLedgerError(
                    "MATRIXPORT_OPENING_NAV_UNAVAILABLE: expected one daily "
                    "account total at subscription recognition"
                )
            matched_nav = matched_nav_rows[0]
            observed_nav = _observed_decimal(
                matched_nav.get("portfolio_value_usd_observed"),
                "matrixport opening portfolio_value_usd_observed",
            )
            strict_nav = _observed_decimal(
                matched_nav.get("portfolio_value_usd_strict"),
                "matrixport opening portfolio_value_usd_strict",
            )
            if (
                matched_nav.get("strict_complete") != "true"
                or observed_nav is None
                or strict_nav is None
                or observed_nav <= 0
                or observed_nav != strict_nav
            ):
                raise ProviderLedgerError(
                    "MATRIXPORT_OPENING_NAV_UNAVAILABLE: first account total "
                    "is partial, absent, non-positive, or inconsistent"
                )
            if observed_nav.quantize(Decimal("0.01")) != amount.quantize(
                Decimal("0.01")
            ):
                raise ProviderLedgerError(
                    "MATRIXPORT_OPENING_NAV_SUBSCRIPTION_MISMATCH"
                )
            anchor["opening_nav"] = {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": format(observed_nav.normalize(), "f"),
            }
            anchor["nav_source_artifact_id"] = nav_artifact_id
        anchors.append(anchor)
    return anchors


def _quantbase_nav_observations(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
    financial_cutoff: date | None = None,
    cutoff_findings: list[dict[str, Any]] | None = None,
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
        if financial_cutoff is not None and day > financial_cutoff:
            _excluded_source_row(
                cutoff_findings,
                "quantbase",
                "nav_observations",
                artifact_id,
                row,
                day,
                financial_cutoff,
            )
            continue
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


def _quantbase_capital_anchors(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
    financial_cutoff: date | None = None,
    cutoff_findings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Admit Quantbase strategy funding without inventing external cash flow.

    A filled fund BUY is an economic strategy allocation.  The dashboard's
    cost-basis increase supplies its later NAV-recognition date, but must not
    overwrite the source activity date or turn the allocation into an
    account-external deposit.
    """
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
        {"time", "cost_basis", "value"},
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
        if financial_cutoff is not None and observed.date() > financial_cutoff:
            _excluded_source_row(
                cutoff_findings,
                "quantbase",
                "capital_anchors",
                activity_artifact.artifact_id,
                row,
                observed.date(),
                financial_cutoff,
            )
            continue
        contributions.append((observed, amount))
    contributions.sort(key=lambda item: item[0])

    history: list[tuple[date, Decimal, Decimal | None]] = []
    for row in price_rows:
        cost_basis = _observed_decimal(row.get("cost_basis"), "quantbase cost basis")
        if cost_basis is None or cost_basis < 0:
            continue
        day = _quantbase_day(row["time"], "quantbase Volatis time")
        if financial_cutoff is not None and day > financial_cutoff:
            _excluded_source_row(
                cutoff_findings,
                "quantbase",
                "nav_observations",
                price_artifact.artifact_id,
                row,
                day,
                financial_cutoff,
            )
            continue
        history.append(
            (
                day,
                cost_basis,
                _observed_decimal(row.get("value"), "quantbase Volatis value"),
            )
        )
    history.sort(key=lambda item: item[0])

    pending: list[tuple[datetime, Decimal]] = []
    contribution_index = 0
    previous_cost_basis = Decimal(0)
    booked_anchors: list[tuple[datetime, Decimal, date, Decimal]] = []
    tolerance = Decimal("0.02")
    for day, cost_basis, nav_value in history:
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
                if nav_value is None or nav_value <= 0:
                    raise ProviderLedgerError(
                        "quantbase capital recognition lacks a positive NAV"
                    )
                booked_anchors.extend(
                    (timestamp, amount, day, nav_value)
                    for timestamp, amount in pending[:matched_count]
                )
                pending = pending[matched_count:]
        previous_cost_basis = cost_basis
    if contribution_index != len(contributions) or pending:
        raise ProviderLedgerError("quantbase capital additions do not reconcile")

    anchors: list[dict[str, Any]] = []
    for index, (timestamp, amount, recognition_day, nav_value) in enumerate(
        sorted(booked_anchors, key=lambda item: item[0])
    ):
        event_day = timestamp.date()
        # Keep the activity timestamp as the economic capital date, but map
        # against the first dashboard NAV that recognizes the allocation.
        strategy_id, mapping_status = _mapped_strategy_for_account(
            account_id, recognition_day, accounts, strategies, assignments
        )
        anchor: dict[str, Any] = {
            "account_id": account_id,
            "amount": {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": format(amount.normalize(), "f"),
            },
            "capital_type": (
                "OPENING_CAPITAL_ANCHOR"
                if index == 0
                else "STRATEGY_CAPITAL_ALLOCATION"
            ),
            "event_date": event_day.isoformat(),
            "evidence_layer": "ACCOUNT_LIVE",
            "generation_id": generation_id,
            "mapping_basis": "RECONCILED_PROVIDER_STRATEGY_CAPITAL",
            "mapping_status": mapping_status,
            "nav_recognition_date": recognition_day.isoformat(),
            "nav_source_artifact_id": f"source/{price_rel.as_posix()}",
            "provider_id": "quantbase",
            "record_id": f"quantbase-capital-anchor:{timestamp.isoformat()}",
            "source_artifact_id": f"source/{activity_rel.as_posix()}",
            "source_record_id": timestamp.isoformat(),
            "strategy_id": strategy_id,
        }
        if index == 0:
            anchor["opening_nav"] = {
                "currency": "USD",
                "status": "AVAILABLE",
                "value": format(nav_value.normalize(), "f"),
            }
        anchors.append(anchor)
    return anchors


def _quantbase_flows(
    generation_id: str,
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    financial_cutoff: date | None = None,
    cutoff_findings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project completed linked-Alpaca cash transfers as account flows.

    Quantbase fund BUY/SELL rows are investment allocations.  They never
    enter this account funding projection.  The captured linked account is an
    Alpaca fiat account; the retained amount/amount_cents pair and Alpaca's
    accepted USD account unit establish USD without applying any crypto parity.
    """
    account_id = _CSV_FACT_ACCOUNT["quantbase"]
    run_id = _run_id(
        _object(catalog.get("legacy_lineage"), "quantbase.legacy_lineage").get(
            "run_id"
        ),
        "quantbase.legacy_lineage.run_id",
    )
    activity_rel = Path("csv/account_activity.csv")
    artifact = _find_manifest_artifact(
        manifest,
        f"source/{activity_rel.as_posix()}",
        "quantbase account activity",
    )
    rows = _verified_csv_rows(
        _exact_file(
            root / "source-runs" / run_id / "artifacts",
            activity_rel,
            "quantbase account activity",
        ),
        artifact.sha256,
        {
            "activity_type",
            "amount",
            "amount_cents",
            "created_at",
            "destination_account_id",
            "id",
            "origin_account_id",
            "status",
            "type",
        },
        "quantbase account activity",
    )
    selected: dict[str, tuple[str, Mapping[str, str], datetime]] = {}
    for row in rows:
        transfer_type = str(row.get("type") or "").upper()
        if (
            row.get("activity_type") != "transfer"
            or transfer_type not in {"DEPOSIT", "WITHDRAWAL"}
            or row.get("status") != "C"
        ):
            continue
        try:
            timestamp = datetime.fromisoformat(
                _text(row.get("created_at"), "quantbase transfer created_at").replace(
                    "Z", "+00:00"
                )
            )
        except ValueError as exc:
            raise ProviderLedgerError(
                "quantbase transfer timestamp is invalid"
            ) from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ProviderLedgerError("quantbase transfer timestamp is invalid")
        if financial_cutoff is not None and timestamp.date() > financial_cutoff:
            _excluded_source_row(
                cutoff_findings,
                "quantbase",
                "flows",
                artifact.artifact_id,
                row,
                timestamp.date(),
                financial_cutoff,
                original_source_dates={"created_at": row["created_at"]},
            )
            continue
        source_id = _text(row.get("id"), "quantbase transfer id")
        row_sha256 = hashlib.sha256(canonical_json_bytes(dict(row))).hexdigest()
        duplicate = selected.get(source_id)
        if duplicate is not None:
            if duplicate[0] != row_sha256:
                raise ProviderLedgerError("conflicting duplicate Quantbase transfer id")
            continue
        selected[source_id] = (row_sha256, row, timestamp)

    projected: list[dict[str, Any]] = []
    linked_account_ids: set[str] = set()
    for source_id, (row_sha256, selected_row, timestamp) in selected.items():
        transfer_type = str(selected_row["type"]).upper()
        amount = _observed_decimal(
            selected_row.get("amount"), "quantbase transfer amount"
        )
        cents = _observed_decimal(
            selected_row.get("amount_cents"), "quantbase transfer amount_cents"
        )
        if (
            amount is None
            or cents is None
            or amount <= 0
            or cents != cents.to_integral_value()
            or cents != amount * 100
        ):
            raise ProviderLedgerError("quantbase transfer amount units differ")
        provider_account_id = _text(
            selected_row.get(
                "destination_account_id"
                if transfer_type == "DEPOSIT"
                else "origin_account_id"
            ),
            "quantbase linked Alpaca account",
        )
        linked_account_ids.add(provider_account_id)
        if len(linked_account_ids) != 1:
            raise ProviderLedgerError("Quantbase linked Alpaca account scope differs")
        if account_id not in accounts:
            raise ProviderLedgerError("quantbase transfer account is unknown")
        signed = amount if selected_row["type"].upper() == "DEPOSIT" else -amount
        projected.append(
            {
                "account_id": account_id,
                "amount": {
                    "currency": "USD",
                    "status": "AVAILABLE",
                    "value": format(signed.normalize(), "f"),
                },
                "capital_disposition": "EXTERNAL_CAPITAL",
                "event_date": timestamp.date().isoformat(),
                "evidence_layer": "ACCOUNT_LIVE",
                "flow_type": "DEPOSIT_OR_WITHDRAWAL",
                "generation_id": generation_id,
                "mapping_basis": "COMPLETED_ALPACA_ACCOUNT_TRANSFER_USD",
                "mapping_status": "UNMAPPED",
                "provider_account_scope": "ACCOUNT_ONLY",
                "provider_account_id_sha256": hashlib.sha256(
                    provider_account_id.encode("utf-8")
                ).hexdigest(),
                "provider_id": "quantbase",
                "record_id": f"quantbase-flow:{source_id}",
                "source_artifact_id": f"source/{activity_rel.as_posix()}",
                "source_record_id": source_id,
                "source_row_sha256": row_sha256,
                "source_timestamp": timestamp.isoformat(),
                "strategy_id": None,
            }
        )
    projected.sort(key=lambda row: (str(row["event_date"]), str(row["record_id"])))
    return projected


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
    allowed = {frozenset(expected)}
    if version == SCHEMA_VERSION:
        optional = ("portfolio_fx_rates", "run_observation")
        allowed.update(
            frozenset({*expected, *subset})
            for subset in (
                (),
                (optional[0],),
                (optional[1],),
                optional,
            )
        )
    if frozenset(spec) not in allowed or version not in {1, SCHEMA_VERSION}:
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
    authority = {
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
    if "run_observation" in spec:
        observation_ref = _object(spec["run_observation"], "run_observation")
        if set(observation_ref) != {"path", "sha256", "size_bytes"}:
            raise ProviderLedgerError("run observation reference keys differ")
        if (
            isinstance(observation_ref["size_bytes"], bool)
            or not isinstance(observation_ref["size_bytes"], int)
            or observation_ref["size_bytes"] < 1
        ):
            raise ProviderLedgerError("run observation size is invalid")
        observation_path = _absolute_regular_path(
            observation_ref["path"], "run_observation.path"
        )
        observation, observation_sha256, observation_size = _verified_json(
            observation_path,
            observation_ref["sha256"],
            "run observation",
        )
        if observation_size != observation_ref["size_bytes"]:
            raise ProviderLedgerError("run observation size differs")
        if (
            set(observation)
            != {
                "schema_version",
                "policy_contract_sha256",
                "financial_cutoff",
                "captured_at_utc",
                "providers",
            }
            or observation.get("schema_version") != 1
        ):
            raise ProviderLedgerError("run observation keys or version differ")
        if observation.get("policy_contract_sha256") != contract_sha256:
            raise ProviderLedgerError("run observation policy contract differs")
        if _day(observation.get("financial_cutoff"), "run observation cutoff") != _day(
            spec["as_of_date"], "spec.as_of_date"
        ):
            raise ProviderLedgerError("run observation cutoff differs from ledger")
        _utc_timestamp(observation.get("captured_at_utc"), "run observation capture")
        selected = _object(observation.get("providers"), "run observation providers")
        configured = _object(spec["providers"], "providers")
        if set(selected) != set(_PROVIDER_CATALOGS):
            raise ProviderLedgerError("run observation provider set differs")
        for provider_id in sorted(_PROVIDER_CATALOGS):
            selection = _object(selected[provider_id], f"{provider_id} observation")
            expected_selection = {
                "root",
                "generation_id",
                "manifest_sha256",
                "catalog_sha256",
                "qualification",
                "generation_as_of_date",
                "finalized_through",
                "finality_evidence",
            }
            if set(selection) != expected_selection:
                raise ProviderLedgerError(
                    f"{provider_id} run observation selection keys differ"
                )
            provider_ref = _object(configured[provider_id], f"providers.{provider_id}")
            if {
                key: selection[key]
                for key in (
                    "root",
                    "generation_id",
                    "manifest_sha256",
                    "catalog_sha256",
                    "qualification",
                )
            } != provider_ref:
                raise ProviderLedgerError(
                    f"{provider_id} run observation selection differs"
                )
            _absolute_path(selection["root"], f"{provider_id} observation root")
            _run_id(selection["generation_id"], f"{provider_id} observation generation")
            _day(
                selection["generation_as_of_date"],
                f"{provider_id} observation generation date",
            )
            _day(selection["finalized_through"], f"{provider_id} observation finality")
            _observed_provider_selection(
                observation,
                provider_id,
                provider_ref,
                _day(spec["as_of_date"], "spec.as_of_date"),
            )
        authority["run_observation"] = observation
        authority["run_observation_path"] = str(observation_path)
        authority["run_observation_sha256"] = observation_sha256
        authority["run_observation_size_bytes"] = observation_size
    return authority


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


def _selected_finality_sources(
    provider_id: str, selected: Mapping[str, Any]
) -> tuple[GenerationManifest, dict[str, Any], Path]:
    root = _absolute_path(selected["root"], "selected provider root")
    generation = root / "generations" / _run_id(selected["generation_id"], "generation")
    manifest, _, _ = _manifest(
        _absolute_regular_path(
            str(generation / "generation.json"), "selected manifest"
        ),
        selected["manifest_sha256"],
        "selected manifest",
    )
    catalog, digest, size = _verified_json(
        _absolute_regular_path(
            str(generation / _PROVIDER_CATALOGS[provider_id][0]), "selected catalog"
        ),
        selected["catalog_sha256"],
        "selected catalog",
    )
    if (
        manifest.generation_id != selected["generation_id"]
        or manifest.as_of_date.isoformat() != selected["generation_as_of_date"]
        or manifest.verification is not VerificationStatus.VERIFIED
        or not any(
            a.sha256 == digest and a.size_bytes == size for a in manifest.artifacts
        )
    ):
        raise ProviderLedgerError("selected finality generation binding differs")
    return manifest, catalog, root


def _bound_source_bytes(
    manifest: GenerationManifest, artifact_id: str, path: Path
) -> bytes:
    artifact = _find_manifest_artifact(manifest, artifact_id, "finality source")
    content, _, size = _verified_evidence_bytes(
        _absolute_regular_path(str(path), "finality source"),
        artifact.sha256,
        "finality source",
    )
    if size != artifact.size_bytes:
        raise ProviderLedgerError("finality source size differs")
    return content


def _schwab_finality_values(
    catalog: Mapping[str, Any],
    artifact_id: str,
    account_id: str | None = None,
) -> dict[date, Decimal]:
    rows = [
        row
        for row in _objects(catalog.get("nav_observations"), "Schwab NAV")
        if row.get("source_artifact_id") == artifact_id
    ]
    accounts = {str(row.get("account_id")) for row in rows}
    if len(accounts) != 1 or (account_id is not None and accounts != {account_id}):
        raise ProviderLedgerError("schwab finality account scope differs")
    values = {
        _day(row["as_of_date"], "Schwab NAV date"): _decimal(
            _object(row["nav"], "Schwab NAV")["value"], "Schwab NAV value"
        )
        for row in rows
    }
    if len(values) != len(rows):
        raise ProviderLedgerError("schwab finality duplicate native labels")
    return values


def _validate_observed_finality_evidence(
    provider_id: str, selected: Mapping[str, Any]
) -> dict[str, Any]:
    """Derive selection evidence from the selected bytes, never a PASS bundle.

    Finalized-through is a policy-selected usable label, not a provider promise
    against restatement. Missing native timezone or capture time stays unknown.
    """
    ref = _object(selected["finality_evidence"], "finality evidence")
    if (
        set(ref) != {"path", "sha256", "size_bytes"}
        or type(ref["size_bytes"]) is not int
        or ref["size_bytes"] < 1
    ):
        raise ProviderLedgerError("finality evidence binding differs")
    path = _absolute_regular_path(ref["path"], "finality evidence")
    content, digest, size = _verified_evidence_bytes(
        path, ref["sha256"], "finality evidence"
    )
    if size != ref["size_bytes"]:
        raise ProviderLedgerError("finality evidence size differs")
    manifest, catalog, root = _selected_finality_sources(provider_id, selected)
    end = _day(selected["finalized_through"], "observation finality")
    if manifest.coverage.observed_end is None or end > manifest.coverage.observed_end:
        raise ProviderLedgerError("finality exceeds selected source boundary")
    result: dict[str, Any] = {
        "basis": "SELECTED_SOURCE_DATE_LABELS",
        "provider_finality_guarantee": False,
        "native_timezone": None,
        "native_calendar": None,
        "capture_start_utc": None,
        "capture_end_utc": None,
    }
    if provider_id in {"alpaca", "fidelity", "portfolio123"}:
        if digest != selected["manifest_sha256"]:
            raise ProviderLedgerError("finite finality must bind the selected manifest")
        result["basis"] = "FINITE_SELECTED_MANIFEST_BOUNDARY"
        return result
    if provider_id == "ibkr":
        from quant_engine.ibkr.flex import parse_flex_file

        artifacts = [
            a for a in manifest.artifacts if a.sha256 == digest and a.size_bytes == size
        ]
        if len(artifacts) != 1:
            raise ProviderLedgerError(
                "ibkr finality statement is not a selected source artifact"
            )
        try:
            document = parse_flex_file(path, artifact_id=artifacts[0].artifact_id)
        except ValueError as exc:
            raise ProviderLedgerError(
                "ibkr finality native statement is invalid"
            ) from exc
        statements = document.statements
        rows = [
            r
            for r in _objects(catalog.get("nav_observations"), "ibkr NAV")
            if r.get("source_artifact_id") == artifacts[0].artifact_id
        ]
        if (
            not rows
            or len({(s.raw_account_id, s.report_date) for s in statements})
            != len(statements)
            or any(s.report_date != end for s in statements)
            or len({r.get("account_id") for r in rows}) != len(rows)
            or any(_day(r.get("as_of_date"), "ibkr NAV date") != end for r in rows)
        ):
            raise ProviderLedgerError(
                "ibkr finality statement account/date scope differs"
            )
        native = sorted((s.base_currency, s.nav) for s in statements)
        normalized = sorted(
            (str(r["nav"]["currency"]), _decimal(r["nav"]["value"], "ibkr NAV"))
            for r in rows
        )
        source_accounts = {
            r["account_id"] for r in _objects(catalog.get("accounts"), "ibkr accounts")
        }
        if native != normalized or not {r["account_id"] for r in rows}.issubset(
            source_accounts
        ):
            raise ProviderLedgerError(
                "ibkr finality native NAV/account binding differs"
            )
        result["basis"] = "NATIVE_FLEX_STATEMENT_LABEL"
        return result
    try:
        evidence = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderLedgerError("native finality evidence is invalid JSON") from exc
    if not isinstance(evidence, dict):
        raise ProviderLedgerError("native finality evidence must be a source object")
    if provider_id == "matrixport":
        run_id = _run_id(catalog.get("source_run_id"), "matrixport source run")
        run_root = root / "output" / run_id
        if (
            _bound_source_bytes(
                manifest,
                "reports/extraction_summary.json",
                run_root / "reports/extraction_summary.json",
            )
            != content
        ):
            raise ProviderLedgerError(
                "matrixport finality capture differs from selected run"
            )
        captured = datetime.fromtimestamp(
            int(evidence["run_end_ms"]) / 1000, tz=timezone.utc
        )
        if (
            evidence.get("status") != "COMPLETED"
            or evidence.get("run_id") != run_id
            or end >= captured.date()
        ):
            raise ProviderLedgerError(
                "matrixport finality does not support prior UTC daily label"
            )
        nav_bytes = _bound_source_bytes(
            manifest, "csv/nav_daily.csv", run_root / "csv/nav_daily.csv"
        )
        labels = set()
        for row in csv.DictReader(io.StringIO(nav_bytes.decode("utf-8-sig"))):
            timestamp = datetime.fromtimestamp(
                int(row["day_timestamp_ms"]) / 1000, tz=timezone.utc
            )
            if timestamp.isoformat().replace("+00:00", "Z") != row[
                "day_timestamp_utc"
            ].replace("+00:00", "Z"):
                raise ProviderLedgerError("matrixport native UTC label differs")
            labels.add(timestamp.date())
        if end not in labels:
            raise ProviderLedgerError(
                "matrixport selected finality daily label is absent"
            )
        result.update(
            basis="PRIOR_UTC_DAILY_LABEL_SELECTION",
            native_timezone="UTC",
            native_calendar="ALWAYS_ON",
        )
        # run_end_ms freezes the request boundary; it is not capture finish time.
        return result
    if provider_id == "quantbase":
        run_id = _run_id(
            _object(catalog.get("legacy_lineage"), "quantbase lineage")["run_id"],
            "quantbase run",
        )
        datasets = _objects(catalog.get("datasets"), "quantbase datasets")
        observed_paths = {str(r["relative_path"]) for r in datasets}
        refreshed = _strings(
            evidence.get("refreshed_datasets"), "quantbase refreshed datasets"
        )
        preserved = _strings(
            evidence.get("preserved_companions"), "quantbase preserved datasets"
        )
        if (
            len(set((*refreshed, *preserved))) != len(refreshed) + len(preserved)
            or {"csv/" + name for name in (*refreshed, *preserved)} != observed_paths
        ):
            raise ProviderLedgerError(
                "quantbase finality inventory differs from selected datasets"
            )
        # The conversion receipt's outputs must be the exact files selected by
        # the generation. Its literal cutoff is disclosed policy, never native
        # timezone or a provider finality guarantee.
        for item in datasets:
            relative = _relative(item["relative_path"], "quantbase dataset")
            artifact_id = "source/" + relative.as_posix()
            selected_bytes = _bound_source_bytes(
                manifest,
                artifact_id,
                root / "source-runs" / run_id / "artifacts" / relative,
            )
            original_bytes = _bound_source_bytes(
                manifest, artifact_id, path.parent / "artifacts" / relative
            )
            if (
                selected_bytes != original_bytes
                or hashlib.sha256(selected_bytes).hexdigest() != item["sha256"]
                or len(selected_bytes) != item["bytes"]
            ):
                raise ProviderLedgerError("quantbase finality dataset binding differs")
        daily = _bound_source_bytes(
            manifest,
            "source/csv/dashboard_Volatis_Strategy_pricehistory.csv",
            path.parent / "artifacts/csv/dashboard_Volatis_Strategy_pricehistory.csv",
        )
        labels = {
            _quantbase_day(r["time"], "Quantbase native label")
            for r in csv.DictReader(io.StringIO(daily.decode("utf-8-sig")))
        }
        intraday = {
            _day(d, "quantbase intraday label")
            for d in _strings(
                evidence.get("intraday_unfinalized_dates"), "quantbase intraday dates"
            )
        }
        if (
            not labels
            or not intraday
            or not intraday.issubset(labels)
            or min(intraday) <= end
            or not labels - intraday
            or max(labels - intraday) != end
            or evidence.get("latest_complete_daily_cutoff") != end.isoformat()
        ):
            raise ProviderLedgerError(
                "quantbase finality selection differs from actual daily labels"
            )
        result["basis"] = "CONVERSION_RECEIPT_POLICY_SELECTED_NATIVE_LABEL"
        return result
    if provider_id == "schwab":
        historical = {
            a.artifact_id: a
            for a in manifest.artifacts
            if a.schema_name == "schwab-wch-historical-balance"
        }
        if not historical:
            raise ProviderLedgerError(
                "schwab finality lacks selected historical NAV sources"
            )
        if "receipt_path" in evidence:
            # Existing recovered native NAV binding proof. Recheck its original
            # receipt and histories; the proof's PASS string is not authority.
            receipt_path = _absolute_regular_path(
                evidence["receipt_path"], "Schwab NAV capture receipt"
            )
            receipt_bytes, _, receipt_size = _verified_evidence_bytes(
                receipt_path, evidence["receipt_sha256"], "Schwab NAV capture receipt"
            )
            if receipt_size != evidence["receipt_size_bytes"]:
                raise ProviderLedgerError("schwab capture receipt size differs")
            captures = _objects(json.loads(receipt_bytes), "Schwab NAV captures")
            checks = _objects(evidence.get("checks"), "Schwab native binding checks")
            used: set[str] = set()
            times = []
            for capture in captures:
                matches = [
                    c for c in checks if c["native_sha256"] == capture["nav_sha256"]
                ]
                if capture.get("http_status") != 200 or len(matches) != 1:
                    raise ProviderLedgerError("schwab native capture binding differs")
                artifacts = [
                    a
                    for a in historical.values()
                    if a.sha256 == matches[0]["normalized_sha256"]
                ]
                if len(artifacts) != 1 or artifacts[0].artifact_id in used:
                    raise ProviderLedgerError(
                        "schwab capture selected account scope differs"
                    )
                artifact_id = artifacts[0].artifact_id
                used.add(artifact_id)
                native_path = receipt_path.parent / (
                    _run_id(capture["alias"], "capture alias") + "-nav.json"
                )
                native_payload, _, _ = _verified_json(
                    _absolute_regular_path(str(native_path), "Schwab native NAV"),
                    capture["nav_sha256"],
                    "Schwab native NAV",
                )
                values = {
                    _quantbase_day(day, "Schwab native date"): _decimal(
                        value["AccountValue"], "Schwab native NAV"
                    )
                    for day, value in _object(
                        native_payload.get("histories"), "Schwab histories"
                    ).items()
                }
                selected_values = _schwab_finality_values(catalog, artifact_id)
                if (
                    not values
                    or len(values) != capture["date_count"]
                    or any(
                        selected_values.get(day) != value
                        for day, value in values.items()
                    )
                    or end not in values
                    or max(values) != end
                ):
                    raise ProviderLedgerError(
                        "schwab native-to-selected NAV values or labels differ"
                    )
                times.append(
                    _utc_timestamp(capture["captured_at_utc"], "Schwab NAV capture")
                )
            if used != set(historical) or len(checks) != len(captures):
                raise ProviderLedgerError("schwab native capture inventory differs")
            result.update(
                capture_start_utc=min(times),
                capture_end_utc=max(times),
                basis="CAPTURED_NATIVE_HISTORICAL_NAV_LABEL",
            )
            return result
        roots = _object(evidence.get("roots"), "Schwab source roots")
        sources = [
            r
            for r in _objects(evidence.get("sources"), "Schwab sources")
            if r.get("role") == "WCH_NAV_CSV"
        ]
        if {r.get("artifact_id") for r in sources} != set(historical) or len(
            sources
        ) != len(historical):
            raise ProviderLedgerError("schwab source finality inventory differs")
        account_ids = {
            r["account_id"]
            for r in _objects(catalog.get("accounts"), "Schwab accounts")
        }
        if {r.get("account_id") for r in sources} != account_ids:
            raise ProviderLedgerError("schwab source finality account scope differs")
        for source in sources:
            artifact_id = source["artifact_id"]
            artifact = historical[artifact_id]
            if (
                source.get("expected_sha256") != artifact.sha256
                or source.get("expected_size_bytes") != artifact.size_bytes
            ):
                raise ProviderLedgerError("schwab source finality artifact differs")
            source_path = _absolute_path(
                roots[source["root_id"]], "Schwab source root"
            ) / _relative(source["relative_path"], "Schwab source path")
            nav_bytes = _bound_source_bytes(manifest, artifact_id, source_path)
            values = {
                _day(r["date"], "Schwab historical label"): _decimal(
                    r["account_value"], "Schwab historical NAV"
                )
                for r in csv.DictReader(io.StringIO(nav_bytes.decode("utf-8-sig")))
            }
            if (
                values
                != _schwab_finality_values(catalog, artifact_id, source["account_id"])
                or end not in values
                or max(values) != end
            ):
                raise ProviderLedgerError(
                    "schwab source finality labels or values differ"
                )
        result["basis"] = "SELECTED_HISTORICAL_NAV_LABEL_CAPTURE_TIME_UNKNOWN"
        return result
    raise ProviderLedgerError("provider finality convention is unsupported")


def _observed_provider_selection(
    run_observation: Mapping[str, Any],
    provider_id: str,
    reference: Mapping[str, Any],
    baseline_as_of: date,
) -> Mapping[str, Any]:
    """Return one hash-bound staged selection without consulting CURRENT."""

    if (
        _day(run_observation.get("financial_cutoff"), "run observation cutoff")
        != baseline_as_of
    ):
        raise ProviderLedgerError("run observation cutoff differs from ledger")
    selected = _object(
        _object(run_observation.get("providers"), "run observation providers").get(
            provider_id
        ),
        f"{provider_id} run observation",
    )
    if set(selected) != {
        "root",
        "generation_id",
        "manifest_sha256",
        "catalog_sha256",
        "qualification",
        "generation_as_of_date",
        "finalized_through",
        "finality_evidence",
    }:
        raise ProviderLedgerError(
            f"{provider_id} run observation selection keys differ"
        )
    selection_keys = (
        "root",
        "generation_id",
        "manifest_sha256",
        "catalog_sha256",
        "qualification",
    )
    if {key: selected.get(key) for key in selection_keys} != {
        key: reference.get(key) for key in selection_keys
    }:
        raise ProviderLedgerError(f"{provider_id} run observation selection differs")
    _validate_observed_finality_evidence(provider_id, selected)
    return selected


def _provider(
    provider_id: str,
    value: object,
    baseline_as_of: date,
    *,
    run_observation: Mapping[str, Any] | None = None,
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
    if manifest.generation_id != generation_id or (
        run_observation is None and manifest.as_of_date > baseline_as_of
    ):
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
    selection = (
        _observed_provider_selection(run_observation, provider_id, ref, baseline_as_of)
        if run_observation is not None
        else None
    )
    if selection is not None and (
        _day(
            selection.get("generation_as_of_date"),
            f"{provider_id} observation generation date",
        )
        != manifest.as_of_date
        or manifest.coverage.observed_end is None
        or _day(
            selection.get("finalized_through"),
            f"{provider_id} observation finality",
        )
        > manifest.coverage.observed_end
    ):
        raise ProviderLedgerError(
            f"{provider_id} observed finality or generation differs"
        )
    if qualification is GenerationQualification.STAGED_COMPLETE:
        if selection is None:
            raise ProviderLedgerError(
                f"{provider_id} staged qualification requires a run observation"
            )
        if (
            manifest.coverage.status
            is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
        ):
            raise ProviderLedgerError(
                f"{provider_id} staged finality or coverage differs"
            )
    elif qualification is GenerationQualification.PUBLISHED_COMPLETE:
        current_path = root / "CURRENT.json"
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


def _portfolio_fx_rates(
    value: object,
    baseline_as_of: date,
) -> tuple[
    str,
    dict[tuple[str, date], Decimal],
    tuple[SourceArtifact, SourceArtifact],
]:
    """Load an explicitly selected, hash-bound daily FX candidate."""

    ref = _object(value, "portfolio_fx_rates")
    expected = {
        "catalog_sha256",
        "generation_id",
        "manifest_sha256",
        "root",
        "selection",
    }
    if set(ref) != expected:
        raise ProviderLedgerError("portfolio FX reference keys differ")
    if ref.get("selection") != "OWNER_SELECTED_YAHOO_DAILY_CLOSE_FOR_MATRIXPORT":
        raise ProviderLedgerError("portfolio FX owner selection is absent")
    root = Path(_text(ref["root"], "portfolio_fx_rates.root")).resolve()
    generation_id = _run_id(ref["generation_id"], "portfolio_fx_rates.generation_id")
    generation_root = root / "generations" / generation_id
    manifest, manifest_hash, manifest_size = _manifest(
        _exact_file(
            generation_root,
            Path("generation.json"),
            "portfolio FX manifest",
        ),
        ref["manifest_sha256"],
        "portfolio FX manifest",
    )
    if (
        manifest.generation_id != generation_id
        or manifest.as_of_date > baseline_as_of
        or manifest.adapter.name != "agent-market-data-terminal-yfinance"
        or manifest.adapter.version != "1"
        or manifest.coverage.status
        is not CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
        or manifest.coverage.fact_types != ("FX_RATE",)
    ):
        raise ProviderLedgerError("portfolio FX generation authority differs")
    catalog, catalog_hash, catalog_size = _verified_json(
        _exact_file(generation_root, Path("catalog.json"), "portfolio FX catalog"),
        ref["catalog_sha256"],
        "portfolio FX catalog",
    )
    catalog_artifact = _find_manifest_artifact(
        manifest, "catalog.json", "portfolio FX catalog"
    )
    rates_artifact = _find_manifest_artifact(
        manifest, "rates.csv", "portfolio FX daily rates"
    )
    if (
        catalog_artifact.sha256 != catalog_hash
        or catalog_artifact.size_bytes != catalog_size
        or catalog.get("catalog_schema_version") != 1
        or catalog.get("source_id") != "portfolio-fx-rates"
        or catalog.get("source_provider") != "yahoo"
        or catalog.get("generation_status") != "CANDIDATE_PENDING_OWNER_SELECTION"
        or catalog.get("selector_authority") != "OWNER_SELECTION_REQUIRED"
        or catalog.get("rates_artifact_id") != "rates.csv"
        or catalog.get("tickers") != ["USDC-USD", "USDT-USD"]
        or catalog.get("quote_convention")
        != {
            "base_currency": "USD",
            "field": "Close",
            "rate_meaning": "USD_PER_ONE_SOURCE_CURRENCY_UNIT",
            "timing": "UTC_DAILY_CLOSE",
        }
    ):
        raise ProviderLedgerError("portfolio FX catalog authority differs")
    rows = _verified_csv_rows(
        _exact_file(generation_root, Path("rates.csv"), "portfolio FX daily rates"),
        rates_artifact.sha256,
        {
            "base_currency",
            "currency",
            "date",
            "field",
            "provider",
            "rate",
            "ticker",
            "timing",
        },
        "portfolio FX daily rates",
    )
    expected_columns = {
        "base_currency",
        "currency",
        "date",
        "field",
        "provider",
        "rate",
        "ticker",
        "timing",
    }
    rates: dict[tuple[str, date], Decimal] = {}
    tickers = {"USDC": "USDC-USD", "USDT": "USDT-USD"}
    for row in rows:
        currency = _text(row.get("currency"), "portfolio FX currency")
        day = _day(row.get("date"), "portfolio FX date")
        rate = _observed_decimal(row.get("rate"), "portfolio FX rate")
        if (
            set(row) != expected_columns
            or currency not in tickers
            or row.get("ticker") != tickers[currency]
            or row.get("base_currency") != "USD"
            or row.get("provider") != "yahoo"
            or row.get("field") != "Close"
            or row.get("timing") != "UTC_DAILY_CLOSE"
            or rate is None
            or rate <= 0
        ):
            raise ProviderLedgerError("portfolio FX rate row differs")
        key = (currency, day)
        if key in rates:
            raise ProviderLedgerError("portfolio FX rate is duplicated")
        rates[key] = rate
    coverage = _object(catalog.get("coverage"), "portfolio FX coverage")
    start = _day(coverage.get("observed_start"), "portfolio FX observed_start")
    end = _day(coverage.get("observed_end"), "portfolio FX observed_end")
    expected_dates = (end - start).days + 1
    if (
        start != manifest.coverage.observed_start
        or end != manifest.coverage.observed_end
        or coverage.get("status") != "COMPLETE_WITHIN_DECLARED_BOUNDARY"
        or coverage.get("known_gaps") != []
        or coverage.get("expected_date_count") != expected_dates
        or coverage.get("rate_count") != len(rates)
        or len(rates) != expected_dates * len(tickers)
        or set(rates)
        != {
            (currency, start + timedelta(days=offset))
            for currency in tickers
            for offset in range(expected_dates)
        }
    ):
        raise ProviderLedgerError("portfolio FX required coverage differs")
    artifacts = (
        SourceArtifact(
            artifact_id="portfolio/fx-rates/generation.json",
            sha256=manifest_hash,
            size_bytes=manifest_size,
            as_of_date=manifest.as_of_date,
            schema_name="generation-manifest",
            schema_version="1",
        ),
        SourceArtifact(
            artifact_id="portfolio/fx-rates/catalog.json",
            sha256=catalog_hash,
            size_bytes=catalog_size,
            as_of_date=manifest.as_of_date,
            schema_name="portfolio-fx-rates-catalog",
            schema_version="1",
        ),
    )
    return generation_id, rates, artifacts


def _assignment_index(
    provenance: Mapping[str, Any],
    financial_contract: Mapping[str, Any] | None = None,
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
    if financial_contract is not None:
        expected_start: date | None = None
        for strategy in _objects(
            financial_contract.get("strategies"), "financial MVP strategies"
        ):
            if strategy.get("strategy_id") != "robuxio-crypto":
                continue
            membership = _object(
                strategy.get("funded_membership"), "Robuxio funded_membership"
            )
            if membership.get("status") != "EVIDENCED":
                break
            expected_start = _day(
                membership.get("effective_start"),
                "Robuxio funded_membership.effective_start",
            )
            break
        if expected_start is None:
            raise ProviderLedgerError("MATRIXPORT_OWNER_FUNDING_BOUNDARY_DIFFERS")
        owner_assignments = [
            item
            for item in assignments.get(_CSV_FACT_ACCOUNT["matrixport"], [])
            if item.get("mapping_status") == "MAPPED"
            and item.get("strategy_id") == "robuxio-crypto"
        ]
        if len(owner_assignments) != 1:
            raise ProviderLedgerError("MATRIXPORT_OWNER_FUNDING_BOUNDARY_DIFFERS")
        owner_assignment = owner_assignments[0]
        if (
            _day(
                owner_assignment.get("effective_start"),
                "Matrixport owner assignment.effective_start",
            )
            != expected_start
            or owner_assignment.get("source_classification")
            != "financial_mvp_funded_membership_reconciled"
        ):
            raise ProviderLedgerError("MATRIXPORT_OWNER_FUNDING_BOUNDARY_DIFFERS")
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


_IBKR_CAPITAL_FLOW_TYPES: Final = (
    "DEPOSIT_OR_WITHDRAWAL",
    "TRANSFER:ACATS:IN",
    "TRANSFER:INTERNAL:IN",
    "TRANSFER:INTERNAL:OUT",
)


def _assignable_strategies(
    account_id: str,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> set[str]:
    """Return the in-perimeter strategies this account is ever assigned to."""

    if account_id not in accounts:
        return set()
    return {
        strategy
        for item in assignments.get(account_id, ())
        for strategy in (item.get("strategy_id"),)
        if isinstance(strategy, str)
        and strategy in strategies
        and item.get("effective_start") not in {None, ""}
    }


def _capital_anchor_epoch_strategy(
    account_id: str,
    day: date,
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[str | None, str]:
    """Resolve the epoch a capital event funds.

    An anchor is a per-strategy fact, so the window that contains the event is
    the first answer.  Capital committed before an account's earliest epoch
    begins is that epoch's opening capital: owner rule HWM-003 makes capital
    continuous across epochs, and an account funded ahead of its strategy start
    opens holding what it was funded with.  A day after an epoch has begun but
    inside no window is a real gap and maps to nothing.
    """

    strategy_id, mapping_status = _mapped_strategy_for_account(
        account_id, day, accounts, strategies, assignments
    )
    if mapping_status == "MAPPED" or account_id not in accounts:
        return strategy_id, mapping_status
    starts: list[tuple[date, str]] = []
    for item in assignments.get(account_id, ()):
        strategy = item.get("strategy_id")
        raw_start = item.get("effective_start")
        if not isinstance(strategy, str) or strategy not in strategies:
            continue
        if raw_start in {None, ""}:
            continue
        starts.append((_day(raw_start, "assignment.effective_start"), strategy))
    if not starts:
        return None, "UNMAPPED"
    first_start = min(start for start, _strategy in starts)
    if day >= first_start:
        return None, "UNMAPPED"
    opening = {strategy for start, strategy in starts if start == first_start}
    if len(opening) != 1:
        return None, "UNMAPPED"
    return opening.pop(), "MAPPED"


def _ibkr_capital_anchors(
    generation_id: str,
    catalog: Mapping[str, Any],
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Net each statement account's capital events on their own event date.

    A single flow row is not a capital event, and the date a row is entered is
    not the date its capital exists.  IBKR credits a deposit advance against a
    pending wire and reverses it later: the advance and its reversal share an
    event date but recognise eight days apart, and the account's NAV rose by
    the advance on the day it recognised.  Netting the recognition date is what
    reproduces the capital NAV actually holds; netting the entry date hides it.
    Small end-of-month adjustments still offset, because they recognise
    together.

    Internal transfers between the owner's own statement accounts are capital
    events for the receiving account even though they are not external capital
    for the household: strategies are mapped to accounts, so an account funded
    by its sibling is funded.  The external/internal distinction stays on the
    flow rows, which carry ``capital_disposition``.

    An account whose capital history contains an amount-less event -- an
    in-kind ACATS transfer -- has no arithmetic that could net its days, so it
    yields no anchors at all rather than a series with a hole in it.
    """

    flow_rows = _objects(catalog.get("flows") or [], "ibkr.flows")
    nav_rows = _objects(catalog.get("nav_observations") or [], "ibkr.nav_observations")
    nav_by_account_day: dict[tuple[str, date], Decimal] = {}
    for row in nav_rows:
        account_id = _text(row.get("account_id"), "ibkr nav account_id")
        day = _day(row.get("as_of_date"), "ibkr nav as_of_date")
        value = _observed_decimal(
            _object(row.get("nav"), "ibkr nav").get("value"), "ibkr nav value"
        )
        if value is None:
            continue
        key = (account_id, day)
        if nav_by_account_day.setdefault(key, value) != value:
            raise ProviderLedgerError("ibkr account NAV has conflicting duplicates")

    # Keyed by recognition date; each entry keeps its own event date so the
    # anchor can still report when the capital was committed.
    by_day: dict[
        str, dict[date, list[tuple[Mapping[str, Any], str, Decimal, date]]]
    ] = defaultdict(lambda: defaultdict(list))
    unquantified: set[str] = set()
    for row in flow_rows:
        if row.get("flow_type") not in _IBKR_CAPITAL_FLOW_TYPES:
            continue
        if row.get("provider_account_scope") != "STATEMENT_ACCOUNT":
            continue
        account_id = _text(row.get("account_id"), "ibkr flow account_id")
        raw_amount = row.get("amount")
        # An in-kind ACATS transfer carries no money at all, and a money
        # observation the provider declares unavailable carries no value.
        # Either way this account has a capital event no arithmetic can net.
        if not isinstance(raw_amount, Mapping):
            unquantified.add(account_id)
            continue
        amount = _observed_decimal(raw_amount, "ibkr flow amount")
        if amount is None:
            unquantified.add(account_id)
            continue
        currency = _text(raw_amount.get("currency"), "ibkr flow currency")
        event_day = _day(row.get("event_date"), "ibkr flow event_date")
        recognition_day = _day(
            row.get("nav_recognition_date") or row["event_date"],
            "ibkr flow nav_recognition_date",
        )
        if recognition_day < event_day:
            raise ProviderLedgerError(
                "ibkr capital event recognises before it is entered"
            )
        by_day[account_id][recognition_day].append((row, currency, amount, event_day))

    anchors: list[dict[str, Any]] = []
    for account_id in sorted(by_day):
        if account_id in unquantified:
            continue
        # An anchor names the strategy it funds.  An account the product never
        # assigns to a strategy has no epoch to name, so it yields no anchors
        # rather than anchors nothing can attribute.
        if not _assignable_strategies(account_id, accounts, strategies, assignments):
            continue
        opened = False
        for day in sorted(by_day[account_id]):
            rows = by_day[account_id][day]
            currencies = {currency for _row, currency, _amount, _event in rows}
            if len(currencies) != 1:
                raise ProviderLedgerError(
                    "ibkr capital events mix currencies on one recognition date"
                )
            currency = currencies.pop()
            net = sum((amount for _row, _currency, amount, _event in rows), Decimal(0))
            if net == 0:
                # An advance and its reversal, or offsetting adjustments.
                continue
            if opened:
                # Only the opening is an anchor.  Unlike Quantbase, whose
                # anchors come from fund activity the ledger does not project
                # as flows, every later IBKR capital movement is already a
                # flow row: emitting it again as a STRATEGY_CAPITAL_ALLOCATION
                # would add the same money to invested capital twice.
                break
            if net < 0:
                raise ProviderLedgerError(
                    "ibkr account returns capital before it is funded"
                )
            # The day's capital is committed once its last component is entered,
            # and recognised on the grouping date.
            recognition_day = day
            event_day = max(event for _row, _currency, _amount, event in rows)
            strategy_id, mapping_status = _capital_anchor_epoch_strategy(
                account_id, recognition_day, accounts, strategies, assignments
            )
            source_record_id = f"{account_id}:{recognition_day.isoformat()}"
            anchor: dict[str, Any] = {
                "account_id": account_id,
                "amount": {
                    "currency": currency,
                    "status": "AVAILABLE",
                    "value": format(net.normalize(), "f"),
                },
                "capital_type": (
                    "OPENING_CAPITAL_ANCHOR"
                    if not opened
                    else "STRATEGY_CAPITAL_ALLOCATION"
                ),
                "event_date": event_day.isoformat(),
                "evidence_layer": "ACCOUNT_LIVE",
                "generation_id": generation_id,
                "mapping_basis": "NET_STATEMENT_ACCOUNT_CAPITAL_ON_PROVIDER_EVENT_DATE",
                "mapping_status": mapping_status,
                "nav_recognition_date": recognition_day.isoformat(),
                "provider_id": "ibkr",
                "record_id": f"ibkr-capital-anchor:{source_record_id}",
                "source_artifact_id": _text(
                    rows[0][0].get("source_artifact_id"),
                    "ibkr flow source_artifact_id",
                ),
                "source_record_id": source_record_id,
                "strategy_id": strategy_id,
                # Every anchor names the artifact its NAV evidence comes from,
                # opening or not: the manifest requires the provenance on the
                # allocation rows too.
                "nav_source_artifact_id": "normalized/ibkr-catalog.json",
            }
            if not opened and net > 0:
                opening_nav = nav_by_account_day.get((account_id, recognition_day))
                if opening_nav is None or opening_nav <= 0:
                    raise ProviderLedgerError(
                        "IBKR_OPENING_NAV_UNAVAILABLE: the account holds no "
                        "positive NAV on the day its capital is recognized"
                    )
                anchor["opening_nav"] = {
                    "currency": currency,
                    "status": "AVAILABLE",
                    "value": format(opening_nav.normalize(), "f"),
                }
                opened = True
            anchors.append(anchor)
    anchors.sort(
        key=lambda item: (str(item["nav_recognition_date"]), str(item["record_id"]))
    )
    return anchors


def _schwab_transactions_within_cutoff(
    rows: Sequence[dict[str, Any]],
    financial_cutoff: date | None,
    cutoff_findings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Bound native transactions before mapping, pairing or valuing them."""
    if financial_cutoff is None:
        return list(rows)
    retained = []
    for row in rows:
        source_dates = {
            field: row[field]
            for field in _DATE_FIELDS
            if row.get(field) not in {None, ""}
        }
        if any(
            _day(value, field) > financial_cutoff
            for field, value in source_dates.items()
        ):
            _excluded_source_row(
                cutoff_findings,
                "schwab",
                "transactions",
                str(row.get("source_artifact_id", "")),
                row,
                _day(next(iter(source_dates.values())), "Schwab source date"),
                financial_cutoff,
                original_source_dates=source_dates,
            )
        else:
            retained.append(row)
    return retained


def _project_account_facts(
    provider_id: str,
    generation_id: str,
    catalog: Mapping[str, Any],
    accounts: set[str],
    strategies: set[str],
    assignments: Mapping[str, Sequence[Mapping[str, Any]]],
    manifest: GenerationManifest | None = None,
    root: Path | None = None,
    fx_rates: tuple[
        str,
        Mapping[tuple[str, date], Decimal],
        tuple[SourceArtifact, SourceArtifact],
    ]
    | None = None,
    financial_cutoff: date | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    projections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exceptions: list[dict[str, Any]] = []
    if provider_id not in _ACCOUNT_FACTS:
        raise ProviderLedgerError(f"unknown provider fact mapping: {provider_id}")
    if provider_id == "ibkr" and financial_cutoff is not None:
        # Anchors consume NAV, recognition-dated flows and ChangeInNAV. Cut all
        # three before reconciliation; a future unquantified flow must not erase
        # an otherwise evidenced opening anchor.
        bounded = dict(catalog)
        for fact_type in ("flows", "nav_observations", "change_in_nav_observations"):
            retained_source = []
            for row in _objects(catalog.get(fact_type, []), f"ibkr.{fact_type}"):
                dates = [
                    _day(row[key], key)
                    for key in (
                        *_DATE_FIELDS,
                        "accounting_date",
                        "report_date",
                        "nav_recognition_date",
                    )
                    if row.get(key) not in {None, ""}
                ]
                if dates and max(dates) > financial_cutoff:
                    _excluded_source_row(
                        exceptions,
                        provider_id,
                        fact_type,
                        str(row.get("source_artifact_id", "")),
                        row,
                        _row_day(row) or max(dates),
                        financial_cutoff,
                    )
                else:
                    retained_source.append(row)
            bounded[fact_type] = retained_source
        catalog = bounded
    for source_fact in _ACCOUNT_FACTS[provider_id]:
        target_fact = "flows" if source_fact == "cash_flows" else source_fact
        if provider_id == "ibkr" and source_fact == "capital_anchors":
            rows = _ibkr_capital_anchors(
                generation_id, catalog, accounts, strategies, assignments
            )
            projections[target_fact].extend(rows)
            for row in rows:
                if row["mapping_status"] == "UNMAPPED":
                    exceptions.append(
                        {
                            "account_in_provenance": row["account_id"] in accounts,
                            "effective_date": row["event_date"],
                            "fact_type": target_fact,
                            "provider_id": provider_id,
                            "record_id": row["record_id"],
                            "status": "UNMAPPED",
                        }
                    )
            continue
        if provider_id in _CSV_FACT_ACCOUNT and source_fact in {
            "capital_anchors",
            "flows",
            "nav_observations",
        }:
            if manifest is None or root is None:
                raise ProviderLedgerError(
                    f"{provider_id} CSV facts require a manifest and root"
                )
            if provider_id == "matrixport":
                rows = (
                    _matrixport_capital_anchors(
                        generation_id,
                        manifest,
                        catalog,
                        root,
                        accounts,
                        strategies,
                        assignments,
                        fx_rates=fx_rates,
                        financial_cutoff=financial_cutoff,
                        cutoff_findings=exceptions,
                    )
                    if source_fact == "capital_anchors"
                    else _matrixport_nav_observations(
                        generation_id,
                        manifest,
                        catalog,
                        root,
                        accounts,
                        strategies,
                        assignments,
                        fx_rates=fx_rates,
                        financial_cutoff=financial_cutoff,
                        cutoff_findings=exceptions,
                    )
                )
            elif source_fact == "flows":
                rows = _quantbase_flows(
                    generation_id,
                    manifest,
                    catalog,
                    root,
                    accounts,
                    strategies,
                    assignments,
                    financial_cutoff=financial_cutoff,
                    cutoff_findings=exceptions,
                )
            elif source_fact == "capital_anchors":
                rows = _quantbase_capital_anchors(
                    generation_id,
                    manifest,
                    catalog,
                    root,
                    accounts,
                    strategies,
                    assignments,
                    financial_cutoff=financial_cutoff,
                    cutoff_findings=exceptions,
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
                    financial_cutoff=financial_cutoff,
                    cutoff_findings=exceptions,
                )
            else:
                raise ProviderLedgerError(
                    f"unsupported CSV fact mapping: {provider_id}.{source_fact}"
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
        if provider_id == "schwab" and source_fact == "transactions":
            rows = _schwab_transactions_within_cutoff(
                rows, financial_cutoff, exceptions
            )
        for row in rows:
            account_id = str(row.get("account_id") or "")
            mapping_row = row
            if provider_id == "schwab" and source_fact == "transactions":
                mapping_row = {
                    "account_id": account_id,
                    "effective_date": row.get("effective_date")
                    or row.get("event_date"),
                }
            day = _row_day(mapping_row)
            if (
                provider_id == "ibkr"
                and source_fact == "flows"
                and row.get("provider_account_scope") != "STATEMENT_ACCOUNT"
            ):
                strategy_id, mapping_status = None, "UNMAPPED"
            else:
                strategy_id, mapping_status = _mapped_strategy(
                    mapping_row, accounts, strategies, assignments
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
                "effective_date",
                "event_date",
                "execution_price",
                "flow_type",
                "kind",
                "market_value",
                "nav_recognition_date",
                "nav",
                "price",
                "quantity",
                "security_id",
                "source_artifact_id",
                "settle_date",
                "provider_activity_id_sha256",
                "provider_account_scope",
                "value_basis",
            ):
                if field in row:
                    record[field] = row[field]
            if (
                source_fact == "flows"
                and record.get("flow_type") == "DEPOSIT_OR_WITHDRAWAL"
            ):
                amount = _observed_decimal(record.get("amount"), "flow amount")
                record["capital_disposition"] = (
                    "NON_CAPITAL" if amount == 0 else "EXTERNAL_CAPITAL"
                )
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
                        "nav_recognition_date": record.get("effective_date")
                        or record.get("event_date"),
                        "record_id": f"schwab-flow:{source_id}",
                    }
                )
            elif provider_id == "schwab" and source_fact == "transactions":
                pinned_journal = _schwab_pinned_journal_external_flow(
                    row,
                    manifest,
                )
                transfer: dict[str, Any] | None = pinned_journal
                if transfer is None and row.get("kind") == "Journaled Shares":
                    transfer = _schwab_share_transfer(row, rows)
                elif (
                    transfer is None
                    and row.get("kind") == "Journal"
                    and re.fullmatch(
                        r"JOURNAL (?:FRM|TO) \.\.\.\d+",
                        str(row.get("description") or ""),
                    )
                ):
                    transfer = {
                        "flow_type": "TRANSFER:INTERNAL:CASH",
                        "mapping_basis": "PROVIDER_CASH_JOURNAL",
                    }
                if transfer is not None:
                    projections["flows"].append(
                        {
                            **record,
                            **transfer,
                            "flow_type": transfer.get(
                                "flow_type", "EXTERNAL_PROVIDER_TRANSFER"
                            ),
                            "mapping_basis": transfer.get(
                                "mapping_basis", "PINNED_RETAINED_JOURNAL"
                            ),
                            "nav_recognition_date": record.get("effective_date")
                            or record.get("event_date"),
                            "record_id": f"schwab-flow:{source_id}",
                        }
                    )

    if provider_id == "matrixport" and fx_rates is not None:
        if manifest is None or root is None:
            raise ProviderLedgerError("matrixport FX facts require a manifest and root")
        flow_rows = _matrixport_flows(
            generation_id,
            manifest,
            catalog,
            root,
            accounts,
            fx_rates,
            financial_cutoff,
            exceptions,
        )
        projections["flows"].extend(flow_rows)
        for row in flow_rows:
            exceptions.append(
                {
                    "account_in_provenance": row["account_id"] in accounts,
                    "effective_date": row["event_date"],
                    "fact_type": "flows",
                    "provider_id": provider_id,
                    "record_id": row["record_id"],
                    "status": "UNMAPPED",
                }
            )

    if financial_cutoff is not None:
        for fact_type, rows in projections.items():
            retained = []
            for row in rows:
                observed = _row_day(row)
                if observed is None or observed <= financial_cutoff:
                    retained.append(row)
                    continue
                exceptions.append(
                    {
                        "financial_cutoff": financial_cutoff.isoformat(),
                        "fact_type": fact_type,
                        "original_record_id": row.get("source_record_id")
                        or row.get("record_id"),
                        "original_source_date": observed.isoformat(),
                        "provider_id": provider_id,
                        "record_id": row.get("record_id"),
                        "source_record_id": row.get("source_record_id"),
                        "status": "OUTSIDE_FINANCIAL_CUTOFF",
                    }
                )
            projections[fact_type] = retained
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


def _returns_within_financial_cutoff(
    rows: Sequence[Mapping[str, Any]],
    provider_id: str,
    financial_cutoff: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep dated provider returns from extending a staged capture past cutoff."""

    retained: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        observed = _day(row.get("date"), "provider return date")
        if observed <= financial_cutoff:
            retained.append(row)
            continue
        findings.append(
            {
                "financial_cutoff": financial_cutoff.isoformat(),
                "fact_type": "provider_strategy_returns",
                "original_record_id": row.get("record_id")
                or f"{provider_id}:{row.get('strategy_id')}:{observed.isoformat()}",
                "original_source_date": observed.isoformat(),
                "provider_id": provider_id,
                "record_id": row.get("record_id")
                or f"{provider_id}:{row.get('strategy_id')}:{observed.isoformat()}",
                "source_record_id": row.get("record_id"),
                "status": "OUTSIDE_FINANCIAL_CUTOFF",
            }
        )
    return retained, findings


def _complete_schwab_security_transfer_values(
    flows: Sequence[dict[str, Any]],
    schwab_catalog: Mapping[str, Any],
    fidelity_catalog: Mapping[str, Any],
    fidelity_generation_id: str,
    required_account_ids: set[str],
    *,
    financial_cutoff: date | None = None,
) -> None:
    """Carry the recorded USD value across a unique, exact security delivery.

    The receiver's blank amount and both native records remain in the proof.
    This values the receipt; it does not infer prior contributed basis, move
    an event date, or classify the two accounts as one product perimeter.
    """
    receipts = _schwab_transactions_within_cutoff(
        _objects(schwab_catalog.get("transactions"), "Schwab transactions"),
        financial_cutoff,
    )
    deliveries = _objects(
        fidelity_catalog.get("corporate_actions"), "Fidelity corporate actions"
    )

    def key(
        row: Mapping[str, Any], *, delivery: bool
    ) -> tuple[str, str, Decimal] | None:
        if row.get("kind") != ("ASSET_TRANSFER" if delivery else "Security Transfer"):
            return None
        if not row.get("symbol") or row.get("quantity") is None:
            return None
        quantity = _decimal(row["quantity"], "security transfer quantity")
        if (delivery and quantity >= 0) or (not delivery and quantity <= 0):
            return None
        return (
            _day(row.get("event_date"), "security transfer date").isoformat(),
            _text(row.get("symbol"), "security transfer symbol"),
            quantity.copy_abs(),
        )

    receipt_index: dict[tuple[str, str, Decimal], list[dict[str, Any]]] = defaultdict(
        list
    )
    delivery_index: dict[tuple[str, str, Decimal], list[dict[str, Any]]] = defaultdict(
        list
    )
    for row in receipts:
        identity = key(row, delivery=False)
        if identity is not None:
            receipt_index[identity].append(row)
    for row in deliveries:
        identity = key(row, delivery=True)
        amount = row.get("amount")
        if (
            identity is not None
            and isinstance(amount, dict)
            and amount.get("currency") == "USD"
            and amount.get("status") == "AVAILABLE"
            and _decimal(amount.get("value"), "security delivery amount") < 0
        ):
            delivery_index[identity].append(row)
    native_by_id = {row["record_id"]: row for row in receipts}
    for flow in flows:
        amount = flow.get("amount")
        if (
            flow.get("provider_id") != "schwab"
            or flow.get("account_id") not in required_account_ids
            or not isinstance(amount, dict)
            or amount != {"currency": "USD", "status": "UNAVAILABLE", "value": None}
        ):
            continue
        native = native_by_id.get(flow.get("source_record_id"))
        if native is None:
            continue
        identity = key(native, delivery=False)
        if (
            identity is None
            or len(receipt_index[identity]) != 1
            or len(delivery_index.get(identity, [])) != 1
        ):
            continue
        if any(
            flow.get(field) != native.get(field)
            for field in ("account_id", "event_date", "quantity", "amount")
        ):
            raise ProviderLedgerError(
                "security receipt projection differs from native record"
            )
        delivery = delivery_index[identity][0]
        recorded_value = _decimal(
            delivery["amount"]["value"], "security delivery amount"
        )
        flow["recorded_amount"] = dict(amount)
        flow["amount"] = {
            "currency": "USD",
            "status": "AVAILABLE",
            "value": format(recorded_value.copy_negate(), "f"),
        }
        flow["transfer_valuation"] = {
            "basis": "RECORDED_OPPOSITE_SECURITY_TRANSFER_VALUE",
            "source_provider_id": "fidelity",
            "source_generation_id": fidelity_generation_id,
            "source_record_id": delivery["record_id"],
            "source_record_sha256": hashlib.sha256(
                canonical_json_bytes(delivery)
            ).hexdigest(),
            "receipt_record_sha256": hashlib.sha256(
                canonical_json_bytes(native)
            ).hexdigest(),
            "event_date": identity[0],
            "symbol": identity[1],
            "quantity": format(identity[2], "f"),
        }


def _schwab_share_transfer(
    row: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Value a provider share movement and identify its exact opposite leg."""
    transfer: dict[str, Any] = {
        "flow_type": "TRANSFER:INTERNAL:SECURITIES",
        "mapping_basis": "PROVIDER_SECURITY_MOVEMENT",
    }
    if row.get("quantity") is None or row.get("execution_price") is None:
        return transfer
    quantity = _decimal(row["quantity"], "Schwab transfer quantity")
    price = _decimal(row["execution_price"], "Schwab transfer price")
    transfer.update(
        amount={
            "currency": "USD",
            "status": "AVAILABLE",
            "value": str(quantity * price),
        },
        value_basis="EVENT_TIME_MARKET_VALUE",
    )
    matches = [
        other
        for other in rows
        if other.get("kind") == "Journaled Shares"
        and other.get("account_id") != row.get("account_id")
        and other.get("security_id") == row.get("security_id")
        and (other.get("effective_date") or other.get("event_date"))
        == (row.get("effective_date") or row.get("event_date"))
        and other.get("quantity") is not None
        and other.get("execution_price") is not None
        and _decimal(other["quantity"], "Schwab transfer quantity") == -quantity
        and _decimal(other["execution_price"], "Schwab transfer price") == price
    ]
    if len(matches) == 1 and row.get("security_id") and quantity != 0:
        transfer.update(
            flow_type="ACCOUNT_TRANSFER",
            mapping_basis="EXACT_OPPOSITE_PROVIDER_SECURITY_MOVEMENT",
            provider_activity_id_sha256=hashlib.sha256(
                canonical_json_bytes(
                    sorted((row["record_id"], matches[0]["record_id"]))
                )
            ).hexdigest(),
        )
    return transfer


def _schwab_pinned_journal_external_flow(
    row: Mapping[str, Any],
    manifest: GenerationManifest | None,
) -> dict[str, str] | None:
    """Classify retained, independently audited Schwab Journal pairs."""

    if str(row.get("kind") or "").strip().upper() != "JOURNAL":
        return None
    effective_date = row.get("effective_date") or row.get("event_date")
    if not isinstance(effective_date, str):
        return None
    try:
        amount = _observed_decimal(row.get("amount"), "Schwab Journal amount")
    except ProviderLedgerError:
        return None
    if amount is None:
        return None
    key = (
        str(row.get("account_id") or ""),
        str(row.get("record_id") or ""),
        effective_date,
        amount,
    )
    pinned = _SCHWAB_PINNED_JOURNAL_EXTERNAL_FLOWS.get(key)
    if pinned is None:
        return None
    if manifest is None:
        raise ProviderLedgerError("pinned Journal source requires a Schwab manifest")
    source_artifact_id = pinned["source_artifact_id"]
    source_artifact_sha256 = pinned["source_artifact_sha256"]
    artifact_by_id = {artifact.artifact_id: artifact for artifact in manifest.artifacts}
    source_artifact = artifact_by_id.get(source_artifact_id)
    if source_artifact is None or source_artifact.sha256 != source_artifact_sha256:
        raise ProviderLedgerError(
            "pinned Journal source artifact is absent or has a different hash"
        )
    return dict(pinned)


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
            str(
                row.get("nav_recognition_date")
                or row.get("event_date")
                or row.get("as_of_date")
                or ""
            ),
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
    *,
    financial_cutoffs: Mapping[str, date] | None = None,
    cutoff_findings: list[dict[str, Any]] | None = None,
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
    selected_identity_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        strategy_id = names.get(row["series_id"])
        if strategy_id is None:
            raise ProviderLedgerError("strategy returns contain an unknown series")
        day = _day(row["date"], "strategy return date")
        provider_id, layer_override = provider_policies[strategy_id]
        source_layer = EvidenceLayer(row["evidence_layer"])
        layer = layer_override or source_layer
        if day > as_of and not (
            financial_cutoffs is not None and layer is EvidenceLayer.ACCOUNT_LIVE
        ):
            raise ProviderLedgerError("strategy return is newer than the ledger")
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
        selected_identity_rows.append(
            {
                "date": day.isoformat(),
                "evidence_layer": source_layer.value,
                "provider_id": provider_id,
                "return": format(_decimal(row["return"], "strategy return"), "f"),
                "strategy_id": strategy_id,
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
    _validate_selected_strategy_evidence(selected_identity_rows, evidence_rows)
    if financial_cutoffs is not None:
        retained = []
        for row in normalized:
            cutoff = min(as_of, financial_cutoffs[str(row["provider_id"])])
            day = _day(row["date"], "strategy return date")
            if row["evidence_layer"] == "ACCOUNT_LIVE" and day > cutoff:
                _excluded_source_row(
                    cutoff_findings,
                    str(row["provider_id"]),
                    "strategy_returns",
                    returns_artifact.artifact_id,
                    row,
                    day,
                    cutoff,
                )
            else:
                retained.append(row)
        normalized = retained
    evidence_artifact = SourceArtifact(
        artifact_id="portfolio/strategy-provider-evidence.json",
        sha256=evidence_hash,
        size_bytes=evidence_size,
        as_of_date=_day(evidence.get("as_of_date"), "strategy evidence as_of_date"),
        schema_name="portfolio-strategy-provider-evidence",
        schema_version="1",
    )
    return normalized, evidence_rows, (returns_artifact, evidence_artifact)


def _validate_selected_strategy_evidence(
    rows: Sequence[Mapping[str, Any]],
    evidence_rows: Sequence[Mapping[str, Any]],
) -> None:
    """Bind source receipts to the exact selected daily return observations."""

    for evidence in evidence_rows:
        account = evidence.get("account")
        if not isinstance(account, Mapping):
            continue
        receipt = account.get("epoch_hpr_receipt")
        if not isinstance(receipt, Mapping):
            continue
        if receipt.get("daily_return_series_schema") != "ordered-date-return/1":
            raise ProviderLedgerError(
                "selected returns differ from strategy evidence: receipt schema differs"
            )
        strategy_id = _text(
            evidence.get("strategy_id"), "strategy evidence strategy_id"
        )
        provider_id = _text(
            evidence.get("provider_id"), "strategy evidence provider_id"
        )
        evidence_layer = _text(
            evidence.get("evidence_layer"), "strategy evidence evidence_layer"
        )
        selected = sorted(
            (
                {
                    "date": _day(row.get("date"), "strategy return date").isoformat(),
                    "return": format(
                        _decimal(row.get("return"), "strategy return"), "f"
                    ),
                }
                for row in rows
                if row.get("strategy_id") == strategy_id
                and row.get("provider_id") == provider_id
                and row.get("evidence_layer") == evidence_layer
            ),
            key=lambda item: item["date"],
        )
        expected_count = receipt.get("daily_return_observations")
        expected_start = receipt.get("observed_start")
        expected_end = receipt.get("observed_end")
        expected_sha256 = receipt.get("daily_return_series_sha256")
        actual_sha256 = hashlib.sha256(canonical_json_bytes(selected)).hexdigest()
        if (
            not selected
            or expected_count != len(selected)
            or expected_start != selected[0]["date"]
            or expected_end != selected[-1]["date"]
            or expected_sha256 != actual_sha256
        ):
            raise ProviderLedgerError(
                "selected returns differ from strategy evidence: daily path identity differs"
            )


def _observed_bounds(rows: Sequence[Mapping[str, Any]]) -> tuple[date, date]:
    days = [day for row in rows if (day := _row_day(row)) is not None]
    if not days:
        raise ProviderLedgerError("admitted provider fact lacks an effective date")
    return min(days), max(days)


def _catalog_decimal_context(function: Callable[..., Any]) -> Callable[..., Any]:
    """Run provider-ledger catalog arithmetic under its fixed Decimal context."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with localcontext() as context:
            context.prec = 28
            context.rounding = ROUND_HALF_EVEN
            return function(*args, **kwargs)

    return wrapped


@_catalog_decimal_context
def _broker_strategy_returns(
    nav_rows: Sequence[Mapping[str, Any]],
    flow_rows: Sequence[Mapping[str, Any]],
    authoritative_provider_ids: set[str],
    *,
    capital_anchors: Sequence[Mapping[str, Any]] = (),
    declared_nav_gap_dates_by_provider: Mapping[str, frozenset[date]] | None = None,
) -> list[dict[str, Any]]:
    """Create flow-adjusted account-live returns from admitted broker NAV.

    A return is emitted only when both adjacent NAV observations map to the same
    accepted strategy. Date-only flows use the documented end-of-day convention.
    Multiple accounts assigned to one strategy are beginning-capital weighted.
    """

    declared_gaps = declared_nav_gap_dates_by_provider or {}
    internal_accounts_by_activity: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in flow_rows:
        if not str(row.get("flow_type") or "").startswith("TRANSFER:INTERNAL"):
            continue
        activity_id = row.get("provider_activity_id_sha256")
        if not isinstance(activity_id, str) or not activity_id:
            continue
        account_id = str(row.get("account_id") or "")
        if account_id:
            internal_accounts_by_activity[
                (str(row.get("provider_id") or ""), activity_id)
            ].add(account_id)
    flows: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    flow_timing_by_provider: dict[str, set[str]] = defaultdict(set)
    for row in flow_rows:
        if row.get("mapping_status") != "MAPPED":
            continue
        provider_id = str(row["provider_id"])
        if provider_id not in authoritative_provider_ids:
            continue
        if (
            provider_id == "ibkr"
            and row.get("provider_account_scope") != "STATEMENT_ACCOUNT"
        ):
            raise ProviderLedgerError("IBKR flow lacks STATEMENT_ACCOUNT scope")
        account_id = str(row["account_id"])
        recognition_day = row.get("nav_recognition_date")
        day = str(recognition_day or row.get("event_date") or "")
        if not day:
            continue
        flow_timing_by_provider[provider_id].add(
            "PROVIDER_NAV_RECOGNITION_DATE"
            if recognition_day is not None
            else "DATE_ONLY_END_OF_DAY"
        )
        raw_amount = row.get("amount")
        amount_missing = raw_amount is None or (
            isinstance(raw_amount, str) and not raw_amount.strip()
        )
        amount = (
            None if amount_missing else _observed_decimal(raw_amount, "flow amount")
        )
        if row.get("capital_disposition") == "NON_CAPITAL":
            if (
                row.get("flow_type") != "DEPOSIT_OR_WITHDRAWAL"
                or amount_missing
                or amount != 0
            ):
                raise ProviderLedgerError("non-capital flow declaration is invalid")
            continue
        if amount_missing:
            raise ProviderLedgerError("mapped flow amount is missing")
        if amount is None:
            activity_id = row.get("provider_activity_id_sha256")
            if str(row.get("flow_type") or "").startswith("TRANSFER:INTERNAL"):
                if (
                    isinstance(activity_id, str)
                    and len(
                        internal_accounts_by_activity.get(
                            (provider_id, activity_id), set()
                        )
                    )
                    > 1
                ):
                    raise ProviderLedgerError(
                        "unresolved cross-account internal transfer amount"
                    )
                # A one-sided/unpaired internal FX row has no usable USD
                # amount. It is retained as evidence but cannot be imputed as
                # either external capital or a zero-valued cash movement.
                continue
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

    quantbase_anchor_adjustments: dict[tuple[str, str, str], Decimal] = defaultdict(
        Decimal
    )
    quantbase_opening_dates: set[tuple[str, str, str]] = set()
    quantbase_anchor_strategies: dict[tuple[str, str, str], str] = {}
    quantbase_opening_accounts: set[tuple[str, str, str]] = set()
    quantbase_allocation_accounts: set[tuple[str, str, str]] = set()
    seen_quantbase_anchor_ids: set[str] = set()
    for row in capital_anchors:
        if row.get("provider_id") != "quantbase":
            continue
        if "quantbase" not in authoritative_provider_ids:
            continue
        record_id = _text(row.get("record_id"), "quantbase capital anchor record_id")
        if record_id in seen_quantbase_anchor_ids:
            raise ProviderLedgerError("duplicate quantbase capital anchor record_id")
        seen_quantbase_anchor_ids.add(record_id)
        account_id = _text(row.get("account_id"), "quantbase capital anchor account_id")
        strategy_id = _text(
            row.get("strategy_id"), "quantbase capital anchor strategy_id"
        )
        if (
            row.get("mapping_status") != "MAPPED"
            or row.get("evidence_layer") != "ACCOUNT_LIVE"
            or row.get("mapping_basis") != "RECONCILED_PROVIDER_STRATEGY_CAPITAL"
        ):
            raise ProviderLedgerError("quantbase capital anchor mapping is unavailable")
        event_day = _day(row.get("event_date"), "quantbase capital anchor event_date")
        recognition_day = _day(
            row.get("nav_recognition_date"),
            "quantbase capital anchor nav_recognition_date",
        )
        if recognition_day < event_day:
            raise ProviderLedgerError(
                "quantbase capital anchor recognition precedes economic event"
            )
        anchor_amount = _object(row.get("amount"), "quantbase capital anchor amount")
        if (
            set(anchor_amount) != {"currency", "status", "value"}
            or anchor_amount.get("currency") != "USD"
        ):
            raise ProviderLedgerError("quantbase capital anchor amount is invalid")
        amount_value = _observed_decimal(
            anchor_amount, "quantbase capital anchor amount"
        )
        if amount_value is None or amount_value <= 0:
            raise ProviderLedgerError("quantbase capital anchor amount is invalid")
        capital_type = row.get("capital_type")
        if capital_type not in {
            "OPENING_CAPITAL_ANCHOR",
            "STRATEGY_CAPITAL_ALLOCATION",
        }:
            raise ProviderLedgerError("quantbase capital anchor type is invalid")
        account_strategy = ("quantbase", account_id, strategy_id)
        anchor_key = ("quantbase", account_id, recognition_day.isoformat())
        previous_strategy = quantbase_anchor_strategies.setdefault(
            anchor_key, strategy_id
        )
        if previous_strategy != strategy_id:
            raise ProviderLedgerError(
                "quantbase capital anchors conflict at NAV recognition date"
            )
        if capital_type == "OPENING_CAPITAL_ANCHOR":
            if account_strategy in quantbase_opening_accounts:
                raise ProviderLedgerError("duplicate quantbase opening capital anchor")
            quantbase_opening_accounts.add(account_strategy)
            quantbase_opening_dates.add(anchor_key)
        else:
            quantbase_allocation_accounts.add(account_strategy)
            quantbase_anchor_adjustments[anchor_key] += amount_value
            flow_timing_by_provider["quantbase"].add("PROVIDER_NAV_RECOGNITION_DATE")

    for account_strategy in quantbase_allocation_accounts:
        if account_strategy not in quantbase_opening_accounts:
            raise ProviderLedgerError(
                "quantbase capital allocation lacks an opening capital anchor"
            )
    for (
        provider_id,
        account_id,
        recognition_day,
    ), strategy_id in quantbase_anchor_strategies.items():
        if not any(
            str(row.get("as_of_date")) == recognition_day
            and row.get("mapping_status") == "MAPPED"
            and row.get("strategy_id") == strategy_id
            for row in by_account.get((provider_id, account_id), [])
        ):
            raise ProviderLedgerError(
                "quantbase capital anchor does not match a mapped recognized NAV"
            )

    contributions: dict[
        tuple[str, str], list[tuple[Decimal, Decimal, str, str, str]]
    ] = defaultdict(list)
    quantbase_return_paths: set[tuple[str, str, str]] = set()
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
            strategy_id = _text(current.get("strategy_id"), "strategy_id")
            if provider_id == "quantbase":
                quantbase_return_paths.add((provider_id, account_id, strategy_id))
            anchor_key = (provider_id, account_id, day)
            if anchor_key in quantbase_opening_dates:
                # An opening anchor establishes the first funded NAV baseline;
                # it is not a period cash flow and must not fabricate a return.
                continue
            net_flow = flows.get((provider_id, account_id, day), Decimal(0))
            net_flow += quantbase_anchor_adjustments.get(anchor_key, Decimal(0))
            period_return = (ending_nav - net_flow) / prior_nav - Decimal(1)
            if period_return <= -1:
                raise ProviderLedgerError("flow-adjusted account return is invalid")
            contributions[(strategy_id, day)].append(
                (
                    prior_nav,
                    period_return,
                    provider_id,
                    account_id,
                    str(current["record_id"]),
                )
            )

    for return_path in quantbase_return_paths:
        if return_path not in quantbase_opening_accounts:
            raise ProviderLedgerError(
                "quantbase mapped NAV lacks exactly one opening capital anchor"
            )

    result: list[dict[str, Any]] = []
    for (strategy_id, day), items in sorted(contributions.items()):
        denominator = sum((item[0] for item in items), Decimal(0))
        if denominator <= 0:
            continue
        # A one-account sleeve is already its own exact return.  Multiplying
        # and dividing it by the same beginning capital applies Decimal
        # context rounding a second time and can change the final digit.
        composite = (
            items[0][1]
            if len(items) == 1
            else sum((capital * value for capital, value, _, _, _ in items), Decimal(0))
            / denominator
        )
        providers = sorted({item[2] for item in items})
        timing_modes = {
            mode
            for provider in providers
            for mode in flow_timing_by_provider.get(provider, {"DATE_ONLY_END_OF_DAY"})
        }
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
                "flow_timing": (
                    next(iter(timing_modes))
                    if len(timing_modes) == 1
                    else "MIXED_PROVIDER_FLOW_TIMING"
                ),
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


def _funded_account_live_boundaries(
    financial_contract: Mapping[str, Any],
) -> dict[str, tuple[date, date]]:
    """Read the accepted funded ACCOUNT_LIVE intervals from product authority."""

    history = _object(
        financial_contract.get("history_corrections"),
        "financial MVP history_corrections",
    )
    boundaries: dict[str, tuple[date, date]] = {}
    for row in _objects(
        history.get("materialized_segment_identities"),
        "financial MVP materialized_segment_identities",
    ):
        if row.get("evidence_layer") != "ACCOUNT_LIVE":
            continue
        strategy_id = _text(row.get("strategy_id"), "funded strategy_id")
        if strategy_id in boundaries:
            raise ProviderLedgerError("funded ACCOUNT_LIVE interval is duplicated")
        observed_start = _day(row.get("observed_start"), "funded observed_start")
        observed_end = _day(row.get("observed_end"), "funded observed_end")
        if observed_end < observed_start:
            raise ProviderLedgerError("funded ACCOUNT_LIVE interval is reversed")
        boundaries[strategy_id] = (observed_start, observed_end)

    required = {
        _text(row.get("strategy_id"), "required financial MVP strategy_id")
        for row in _objects(
            financial_contract.get("strategies"), "financial MVP strategies"
        )
        if row.get("product_requirement") == "REQUIRED"
    }
    if set(boundaries) != required:
        raise ProviderLedgerError("funded ACCOUNT_LIVE strategy boundary differs")
    return boundaries


def _provider_native_account_return_strategy_ids(
    financial_contract: Mapping[str, Any],
) -> set[str]:
    """Select account-return paths whose materialized authority is a provider."""

    history = _object(
        financial_contract.get("history_corrections"),
        "financial MVP history_corrections",
    )
    native_source_ids = frozenset(_PROVIDER_SOURCE_SEGMENTS.values())
    return {
        _text(row.get("strategy_id"), "provider-native strategy_id")
        for row in _objects(
            history.get("materialized_segment_identities"),
            "financial MVP materialized_segment_identities",
        )
        if row.get("evidence_layer") == "ACCOUNT_LIVE"
        and row.get("source_id") in native_source_ids
    }


def _validate_provider_native_strategy_evidence(
    evidence_rows: Sequence[Mapping[str, Any]],
    strategy_ids: set[str] | frozenset[str],
    manifests: Mapping[str, GenerationManifest],
) -> None:
    """Require provider-native evidence to name exact current manifest artifacts."""

    evidence_by_strategy = {
        _text(row.get("strategy_id"), "strategy evidence strategy_id"): row
        for row in evidence_rows
    }
    for strategy_id in sorted(strategy_ids):
        row = evidence_by_strategy.get(strategy_id)
        if row is None:
            raise ProviderLedgerError(
                f"provider-native strategy evidence is absent: {strategy_id}"
            )
        provider_id = _text(
            row.get("provider_id"), "provider-native evidence provider_id"
        )
        manifest = manifests.get(provider_id)
        if manifest is None or row.get("generation_id") != manifest.generation_id:
            raise ProviderLedgerError(
                "provider-native strategy evidence generation differs"
            )
        artifacts = {artifact.artifact_id: artifact for artifact in manifest.artifacts}
        sources = _object(row.get("sources"), "provider-native evidence sources")
        bound_artifact_count = 0
        for source in sources.values():
            source_row = _object(source, "provider-native evidence source")
            artifact_id = source_row.get("artifact_id")
            if artifact_id is None:
                continue
            artifact = artifacts.get(
                _text(artifact_id, "provider-native evidence artifact_id")
            )
            if artifact is None:
                raise ProviderLedgerError(
                    "provider-native evidence artifact is absent from current manifest"
                )
            if (
                source_row.get("sha256") != artifact.sha256
                or source_row.get("size_bytes") != artifact.size_bytes
            ):
                raise ProviderLedgerError(
                    "provider-native evidence provider artifact identity differs"
                )
            bound_artifact_count += 1
        if bound_artifact_count == 0:
            raise ProviderLedgerError(
                "provider-native strategy evidence lacks a current manifest artifact"
            )


def _account_live_returns_within_funded_boundaries(
    rows: Sequence[Mapping[str, Any]],
    boundaries: Mapping[str, tuple[date, date]],
) -> list[Mapping[str, Any]]:
    """Keep required strategy returns only inside accepted funded intervals."""

    result: list[Mapping[str, Any]] = []
    for row in rows:
        strategy_id = _text(row.get("strategy_id"), "strategy return strategy_id")
        boundary = boundaries.get(strategy_id)
        if row.get("evidence_layer") != "ACCOUNT_LIVE" or boundary is None:
            result.append(row)
            continue
        observed = _day(row.get("date"), "strategy return date")
        if boundary[0] <= observed <= boundary[1]:
            result.append(row)
    return result


_ROLLFORWARD_FIELDS: Final = (
    "account_reconciliation_sha256",
    "beginning_capital",
    "component_account_count",
    "component_account_ids",
    "flow_timing",
    "source_record_ids",
)
_ROLLFORWARD_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


def _complete_broker_rollforward(row: Mapping[str, Any]) -> bool:
    """Require a usable complete broker rollforward before enriching a return."""

    digest = row.get("account_reconciliation_sha256")
    if not isinstance(digest, str) or _ROLLFORWARD_SHA256.fullmatch(digest) is None:
        return False
    try:
        beginning_capital = _decimal(
            row.get("beginning_capital"), "broker beginning capital"
        )
    except ProviderLedgerError:
        return False
    if beginning_capital <= 0:
        return False
    account_ids = row.get("component_account_ids")
    source_record_ids = row.get("source_record_ids")
    component_count = row.get("component_account_count")
    flow_timing = row.get("flow_timing")
    return (
        isinstance(account_ids, list)
        and bool(account_ids)
        and all(isinstance(value, str) and value.strip() for value in account_ids)
        and len(set(account_ids)) == len(account_ids)
        and isinstance(component_count, int)
        and not isinstance(component_count, bool)
        and component_count > 0
        and component_count == len(account_ids)
        and isinstance(flow_timing, str)
        and bool(flow_timing.strip())
        and isinstance(source_record_ids, list)
        and bool(source_record_ids)
        and all(isinstance(value, str) and value.strip() for value in source_record_ids)
    )


def _merge_account_returns(
    source_rows: Sequence[Mapping[str, Any]],
    broker_rows: Sequence[Mapping[str, Any]],
    strategy_names: Mapping[str, str],
    *,
    provider_native_strategy_ids: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Apply the contract-selected account-return authority for each strategy."""

    result = [
        dict(item)
        for item in source_rows
        if not (
            item.get("evidence_layer") == "ACCOUNT_LIVE"
            and str(item.get("strategy_id")) in provider_native_strategy_ids
        )
    ]
    retained_account_rows = {
        (str(row["strategy_id"]), str(row["date"])): row
        for row in result
        if row.get("evidence_layer") == "ACCOUNT_LIVE"
    }
    last_source: dict[str, str] = {}
    for row in result:
        if row.get("evidence_layer") == "ACCOUNT_LIVE":
            strategy_id = str(row["strategy_id"])
            last_source[strategy_id] = max(
                last_source.get(strategy_id, ""), str(row["date"])
            )
    for broker_row in broker_rows:
        strategy_id = str(broker_row["strategy_id"])
        if strategy_id not in provider_native_strategy_ids and str(
            broker_row["date"]
        ) <= last_source.get(strategy_id, ""):
            retained = retained_account_rows.get((strategy_id, str(broker_row["date"])))
            if retained is not None and _complete_broker_rollforward(broker_row):
                retained.update(
                    {field: broker_row[field] for field in _ROLLFORWARD_FIELDS}
                )
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
    *,
    provider_native_strategy_ids: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Retain explicit parity evidence and state which overlapping path won."""

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
                    "CONTRACT_SELECTED_PROVIDER_GENERATION; retained tracker path "
                    "is comparison evidence only"
                    if strategy_id in provider_native_strategy_ids
                    else "METHOD_AND_FLOW_BOUNDARY_VARIANCE; accepted tracker "
                    "account path retains its correction set; broker NAV path is "
                    "retained as reconciliation evidence and rollforward input only"
                ),
                "authority_effect": (
                    "CURRENT_PROVIDER_ACCOUNT_PATH_PROMOTED; "
                    "RETAINED_ACCOUNT_PATH_SUPERSEDED"
                    if strategy_id in provider_native_strategy_ids
                    else "INHERITED_ACCOUNT_PATH_RETAINED; BROKER_OVERLAP_NOT_PROMOTED"
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
        else:
            # A prior receipt may describe account-live evidence that is no
            # longer admitted by the current source facts. Do not retain that
            # stale account scope on a provider-only refreshed receipt.
            refreshed_row.pop("account", None)
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
        equity_observation = (equity_value, cash_added)
        prior_observation = equity_observations[entity_id].get(observed)
        if prior_observation is not None and prior_observation != equity_observation:
            raise ProviderLedgerError(
                "Portfolio123 equity curve has conflicting duplicate dates"
            )
        equity_observations[entity_id][observed] = equity_observation

    for row in performance_table.to_pylist():
        entity_id = str(row.get("_entity_id") or "")
        if entity_id not in selected:
            continue
        field = _text(row.get("field"), "Portfolio123 managed performance field")
        selected_value = _text(
            row.get("value"), "Portfolio123 managed performance value"
        )
        prior_selected_value = selected[entity_id].get(field)
        if prior_selected_value is not None and prior_selected_value != selected_value:
            raise ProviderLedgerError(
                "Portfolio123 managed performance has conflicting captures"
            )
        selected[entity_id][field] = selected_value

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
        # Each observation is [marker, Strategy, Total Value]: `panels` names
        # the two graphs, Strategy at idx 0 and Total Value at idx 1, and the
        # leading element is a marker outside that indexing. The reconciliation
        # curve carries the same pair in its own two columns, so the existing
        # element-wise check against it holds; only the performance level was
        # read from the wrong element. Taking the marker as the level collapsed
        # every return onto the three values the marker takes.
        parsed: list[tuple[date, Decimal, Decimal, Decimal]] = []
        for observed, raw in zip(calendar, data, strict=True):
            if not isinstance(raw, list) or len(raw) != 3:
                raise ProviderLedgerError(
                    "Portfolio123 managed performance observation differs"
                )
            level = _decimal(raw[1], "Portfolio123 managed strategy level")
            total_value = _decimal(raw[2], "Portfolio123 managed total value")
            if level <= 0 or total_value < 0:
                raise ProviderLedgerError(
                    "Portfolio123 managed performance contains invalid values"
                )
            parsed.append((observed, level, level, total_value))
        if len(parsed) < 2:
            raise ProviderLedgerError(
                f"Portfolio123 provider path is unavailable: {entity_id}"
            )
        ordered_equity = sorted(equity_observations[entity_id].items())
        overlap = 0
        for observed, _level, curve_level, curve_total in parsed:
            if observed not in equity_observations[entity_id]:
                continue
            overlap += 1
            if equity_observations[entity_id][observed] != (curve_level, curve_total):
                raise ProviderLedgerError(
                    "Portfolio123 managed performance and equity differ"
                )
        if len(ordered_equity) < 2 or overlap < 2:
            raise ProviderLedgerError(
                "Portfolio123 managed performance/equity overlap is insufficient"
            )
        for managed_previous, managed_current in zip(parsed, parsed[1:], strict=False):
            period_return = managed_current[1] / managed_previous[1] - Decimal(1)
            if period_return <= -1:
                raise ProviderLedgerError("Portfolio123 provider return is invalid")
            result.append(
                {
                    "cost": None,
                    "date": managed_current[0].isoformat(),
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


def _quantbase_csv_cell(value: object) -> str:
    """Match the retained HAR-to-CSV converter's scalar projection."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _quantbase_flow_coverage(
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    known_accounts: set[str],
) -> dict[str, dict[str, Any]]:
    """Prove the account-activity CSV is the complete captured API response."""

    artifact_ids = (
        "source/csv/account_activity.csv",
        "source/har/quantbase_20260902_171237.har",
        ("source/har_output/responses/0274_qbapi3.com_200_application_json.json"),
    )
    declared_artifact_ids = {artifact.artifact_id for artifact in manifest.artifacts}
    raw_capture_ids = set(artifact_ids[1:])
    declared_raw_capture_ids = declared_artifact_ids & raw_capture_ids
    if not declared_raw_capture_ids:
        return {}
    if declared_raw_capture_ids != raw_capture_ids:
        raise ProviderLedgerError(
            "Quantbase account activity capture artifact set is incomplete"
        )

    account_id = _CSV_FACT_ACCOUNT["quantbase"]
    if known_accounts != {account_id}:
        raise ProviderLedgerError("Quantbase account activity account scope differs")
    run_id = _run_id(
        _object(catalog.get("legacy_lineage"), "quantbase.legacy_lineage").get(
            "run_id"
        ),
        "quantbase.legacy_lineage.run_id",
    )
    run_root = root / "source-runs" / run_id / "artifacts"
    verified: dict[str, dict[str, Any]] = {}
    contents: dict[str, bytes] = {}
    for artifact_id in artifact_ids:
        artifact = _find_manifest_artifact(
            manifest, artifact_id, "Quantbase account activity capture"
        )
        relative = Path(artifact_id.removeprefix("source/"))
        path = _exact_file(run_root, relative, "Quantbase account activity capture")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ProviderLedgerError(
                "Quantbase account activity capture is not readable"
            ) from exc
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ProviderLedgerError("Quantbase account activity capture hash differs")
        contents[artifact_id] = content

    har_id, response_id = artifact_ids[1], artifact_ids[2]
    for artifact_id in (har_id, response_id):
        try:
            value = json.loads(contents[artifact_id].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderLedgerError(
                "Quantbase account activity capture is not readable JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ProviderLedgerError(
                "Quantbase account activity capture must be an object"
            )
        verified[artifact_id] = value

    har = verified[har_id]
    log = har.get("log")
    if not isinstance(log, Mapping):
        raise ProviderLedgerError("Quantbase account activity HAR shape differs")
    entries = log.get("entries")
    if not isinstance(entries, list):
        raise ProviderLedgerError("Quantbase account activity HAR shape differs")
    matches: list[Mapping[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        request = entry.get("request")
        response = entry.get("response")
        if not isinstance(request, Mapping) or not isinstance(response, Mapping):
            continue
        raw_url = request.get("url")
        if not isinstance(raw_url, str):
            continue
        parsed_url = urlsplit(raw_url)
        if (
            request.get("method") == "GET"
            and parsed_url.scheme == "https"
            and parsed_url.hostname == "qbapi3.com"
            and re.fullmatch(r"/api/users/[^/]+/accounts/activity/", parsed_url.path)
            and parsed_url.query == ""
            and request.get("queryString") == []
            and response.get("status") == 200
        ):
            matches.append(entry)
    if len(matches) != 1:
        raise ProviderLedgerError(
            "Quantbase account activity HAR request is missing or ambiguous"
        )
    match = matches[0]
    try:
        capture_observed_at = datetime.fromisoformat(
            _text(
                match.get("startedDateTime"),
                "Quantbase account activity startedDateTime",
            ).replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ProviderLedgerError(
            "Quantbase account activity capture time is invalid"
        ) from exc
    if (
        capture_observed_at.tzinfo is None
        or capture_observed_at.utcoffset() is None
        or capture_observed_at.date() != manifest.as_of_date
    ):
        raise ProviderLedgerError("Quantbase account activity capture date differs")

    response = match["response"]
    content = response.get("content")
    if not isinstance(content, Mapping) or not isinstance(content.get("text"), str):
        raise ProviderLedgerError("Quantbase account activity HAR response is missing")
    response_text = content["text"]
    if content.get("encoding") == "base64":
        try:
            response_text = base64.b64decode(response_text, validate=True).decode(
                "utf-8"
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderLedgerError(
                "Quantbase account activity HAR response encoding differs"
            ) from exc
    elif content.get("encoding") not in {None, ""}:
        raise ProviderLedgerError(
            "Quantbase account activity HAR response encoding differs"
        )
    try:
        har_response = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ProviderLedgerError(
            "Quantbase account activity HAR response is not JSON"
        ) from exc
    captured_response = verified[response_id]
    if har_response != captured_response:
        raise ProviderLedgerError(
            "Quantbase account activity response differs from HAR"
        )
    if set(captured_response) != {"data"} or not isinstance(
        captured_response["data"], list
    ):
        raise ProviderLedgerError("Quantbase account activity response shape differs")
    response_rows = captured_response["data"]
    if not response_rows:
        raise ProviderLedgerError("Quantbase account activity response is empty")

    normalized: list[tuple[str, Mapping[str, Any]]] = []
    fields = {"activity_type"}
    observed_times: list[datetime] = []
    timestamp_fields = (
        "time",
        "date",
        "completed_at",
        "filled_at",
        "created_at",
        "submitted_at",
        "updated_at",
        "canceled_at",
    )
    for index, response_row in enumerate(response_rows):
        if not isinstance(response_row, Mapping) or not isinstance(
            response_row.get("type"), str
        ):
            raise ProviderLedgerError(
                f"Quantbase account activity row {index} shape differs"
            )
        item = response_row.get("item")
        if not isinstance(item, Mapping) or any(
            not isinstance(field, str) for field in item
        ):
            raise ProviderLedgerError(
                f"Quantbase account activity row {index} item differs"
            )
        fields.update(item)
        for field in timestamp_fields:
            raw_timestamp = item.get(field)
            if raw_timestamp is None or raw_timestamp == "":
                continue
            if not isinstance(raw_timestamp, str):
                raise ProviderLedgerError(
                    "Quantbase account activity timestamp is invalid"
                )
            try:
                parsed_timestamp = datetime.fromisoformat(
                    raw_timestamp.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ProviderLedgerError(
                    "Quantbase account activity timestamp is invalid"
                ) from exc
            if (
                parsed_timestamp.tzinfo is None
                or parsed_timestamp.utcoffset() is None
                or parsed_timestamp > capture_observed_at
            ):
                raise ProviderLedgerError(
                    "Quantbase account activity timestamp is invalid"
                )
            observed_times.append(parsed_timestamp)
        normalized.append((response_row["type"], item))
    if not observed_times:
        raise ProviderLedgerError("Quantbase account activity date coverage is empty")

    try:
        csv_text = contents[artifact_ids[0]].decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProviderLedgerError(
            "Quantbase account activity CSV is not readable UTF-8"
        ) from exc
    try:
        csv_rows = list(csv.reader(io.StringIO(csv_text, newline=""), strict=True))
    except csv.Error as exc:
        raise ProviderLedgerError("Quantbase account activity CSV is invalid") from exc
    if (
        not csv_rows
        or len(csv_rows[0]) != len(set(csv_rows[0]))
        or set(csv_rows[0]) != fields
    ):
        raise ProviderLedgerError(
            "Quantbase account activity CSV differs from captured response"
        )
    header = csv_rows[0]
    expected_rows = [
        [
            _quantbase_csv_cell(
                activity_type if field == "activity_type" else item.get(field)
            )
            for field in header
        ]
        for activity_type, item in normalized
    ]
    if csv_rows[1:] != expected_rows:
        raise ProviderLedgerError(
            "Quantbase account activity CSV differs from captured response"
        )

    return {
        account_id: {
            "capture_observed_at": capture_observed_at.isoformat(),
            "effective_end": capture_observed_at.date().isoformat(),
            "effective_start": min(observed_times).date().isoformat(),
            "record_count": len(response_rows),
            "source_artifact_ids": sorted(artifact_ids),
        }
    }


def _matrixport_flow_coverage(
    manifest: GenerationManifest,
    catalog: Mapping[str, Any],
    root: Path,
    known_accounts: set[str],
) -> dict[str, dict[str, Any]]:
    """Prove the retained wallet history through its observed boundary."""

    artifact_ids = (
        "csv/coverage.csv",
        "csv/wallet_currency_dispositions.csv",
        "csv/wallet_deposits.csv",
        "reports/request_manifest.jsonl",
    )
    declared_artifact_ids = {artifact.artifact_id for artifact in manifest.artifacts}
    if artifact_ids[0] not in declared_artifact_ids:
        return {}
    if not set(artifact_ids).issubset(declared_artifact_ids):
        raise ProviderLedgerError(
            "Matrixport wallet history capture artifact set is incomplete"
        )

    account_id = _CSV_FACT_ACCOUNT["matrixport"]
    if known_accounts != {account_id}:
        raise ProviderLedgerError("Matrixport wallet history account scope differs")
    source_run_id = _run_id(catalog.get("source_run_id"), "matrixport.source_run_id")
    run_root = root / "output" / source_run_id
    verified_paths: dict[str, Path] = {}
    for artifact_id in artifact_ids:
        artifact = _find_manifest_artifact(
            manifest, artifact_id, "Matrixport wallet history capture"
        )
        path = _exact_file(
            run_root, Path(artifact_id), "Matrixport wallet history capture"
        )
        _verify_hash(path, artifact.sha256, "Matrixport wallet history capture")
        verified_paths[artifact_id] = path

    coverage_rows = _read_csv_rows(
        verified_paths[artifact_ids[0]],
        {"details_json", "endpoint", "status", "terminal_reason", "updated_at_utc"},
        "Matrixport wallet coverage",
    )
    endpoint_counts: dict[str, int | None] = {
        "GET https://mapi.matrixport.com/mapi/v1/wallet/deposits": None,
        "GET https://mapi.matrixport.com/mapi/v1/wallet/withdrawals": None,
    }
    capture_times: list[datetime] = []
    for endpoint in endpoint_counts:
        matches = [row for row in coverage_rows if row.get("endpoint") == endpoint]
        if len(matches) != 1:
            raise ProviderLedgerError(
                "Matrixport wallet coverage endpoint is missing or ambiguous"
            )
        row = matches[0]
        if (
            row.get("status") != "complete_to_observed_boundary"
            or row.get("terminal_reason")
            != "structured_parameter_rejections_dispositioned"
        ):
            raise ProviderLedgerError(
                "Matrixport wallet history coverage is incomplete"
            )
        try:
            details = json.loads(row["details_json"])
        except json.JSONDecodeError as exc:
            raise ProviderLedgerError(
                "Matrixport wallet coverage details are not JSON"
            ) from exc
        raw_rows = details.get("raw_rows") if isinstance(details, Mapping) else None
        if not isinstance(raw_rows, int) or isinstance(raw_rows, bool) or raw_rows < 0:
            raise ProviderLedgerError("Matrixport wallet coverage count is invalid")
        endpoint_counts[endpoint] = raw_rows
        try:
            capture_time = datetime.fromisoformat(
                _text(
                    row.get("updated_at_utc"),
                    "Matrixport wallet coverage updated_at_utc",
                ).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ProviderLedgerError(
                "Matrixport wallet coverage time is invalid"
            ) from exc
        if (
            capture_time.tzinfo is None
            or capture_time.utcoffset() is None
            or capture_time.utcoffset() != timedelta(0)
        ):
            raise ProviderLedgerError("Matrixport wallet coverage time is not UTC")
        capture_times.append(capture_time)

    wallet_rows = _read_csv_rows(
        verified_paths[artifact_ids[2]],
        {"created_at_utc", "state", "transaction_id"},
        "Matrixport wallet deposits",
    )
    deposit_times: list[datetime] = []
    for row in wallet_rows:
        if str(row.get("state") or "").lower() not in {
            "completed",
            "confirmed",
            "success",
        }:
            raise ProviderLedgerError(
                "Matrixport wallet deposits contain a non-confirmed row"
            )
        _text(row.get("transaction_id"), "Matrixport wallet transaction_id")
        try:
            event_time = datetime.fromisoformat(
                _text(
                    row.get("created_at_utc"),
                    "Matrixport wallet created_at_utc",
                ).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ProviderLedgerError(
                "Matrixport wallet deposit time is invalid"
            ) from exc
        if (
            event_time.tzinfo is None
            or event_time.utcoffset() is None
            or event_time.utcoffset() != timedelta(0)
        ):
            raise ProviderLedgerError("Matrixport wallet deposit time is not UTC")
        deposit_times.append(event_time)

    deposit_count = endpoint_counts[
        "GET https://mapi.matrixport.com/mapi/v1/wallet/deposits"
    ]
    withdrawal_count = endpoint_counts[
        "GET https://mapi.matrixport.com/mapi/v1/wallet/withdrawals"
    ]
    if deposit_count != len(wallet_rows) or withdrawal_count != 0:
        raise ProviderLedgerError("Matrixport wallet history count differs")
    if not deposit_times:
        raise ProviderLedgerError("Matrixport wallet history date coverage is empty")

    observed_boundary = _day(
        catalog.get("observed_boundary_date"),
        "matrixport observed_boundary_date",
    )
    capture_observed_at = max(capture_times)
    if capture_observed_at.date() != observed_boundary:
        raise ProviderLedgerError("Matrixport wallet coverage boundary differs")
    if max(deposit_times) > capture_observed_at:
        raise ProviderLedgerError("Matrixport wallet deposit postdates its capture")
    return {
        account_id: {
            "capture_observed_at": capture_observed_at.isoformat(),
            "effective_end": observed_boundary.isoformat(),
            "effective_start": min(deposit_times).date().isoformat(),
            "record_count": len(wallet_rows),
            "source_artifact_ids": sorted(artifact_ids),
        }
    }


def _ibkr_flow_coverage(
    catalog: Mapping[str, Any], known_accounts: set[str]
) -> dict[str, dict[str, Any]]:
    matches = [
        item
        for item in _objects(catalog.get("fact_coverage"), "IBKR fact coverage")
        if item.get("fact_type") == "FLEX_FINALIZED_HISTORY"
    ]
    if len(matches) != 1:
        raise ProviderLedgerError("IBKR finalized history coverage is missing")
    history = matches[0]
    _strict(
        history,
        {"accounts", "calendar", "fact_type", "known_gaps", "status"},
        "IBKR finalized history coverage",
    )
    if (
        history["calendar"] != "WEEKDAY_SOURCE_CALENDAR"
        or history["status"] != "COMPLETE_WITHIN_DECLARED_BOUNDARY"
        or history["known_gaps"] != []
    ):
        raise ProviderLedgerError("IBKR finalized history coverage is incomplete")
    result: dict[str, dict[str, Any]] = {}
    for raw_account in _objects(history["accounts"], "IBKR coverage accounts"):
        _strict(
            raw_account,
            {"account_id", "intervals", "statement_count"},
            "IBKR coverage account",
        )
        account_id = _text(raw_account["account_id"], "IBKR coverage account_id")
        if account_id not in known_accounts or account_id in result:
            raise ProviderLedgerError("IBKR finalized history account scope differs")
        statement_count = raw_account["statement_count"]
        if (
            not isinstance(statement_count, int)
            or isinstance(statement_count, bool)
            or statement_count < 1
        ):
            raise ProviderLedgerError("IBKR finalized history count is invalid")
        intervals = _objects(raw_account["intervals"], "IBKR coverage intervals")
        if len(intervals) != 1:
            raise ProviderLedgerError("IBKR finalized history is not contiguous")
        interval = intervals[0]
        _strict(
            interval,
            {"effective_end", "effective_start", "source_artifact_ids"},
            "IBKR coverage interval",
        )
        start = _day(interval["effective_start"], "IBKR flow coverage start")
        end = _day(interval["effective_end"], "IBKR flow coverage end")
        artifact_ids = interval["source_artifact_ids"]
        if (
            start > end
            or not isinstance(artifact_ids, list)
            or not artifact_ids
            or any(not isinstance(item, str) or not item for item in artifact_ids)
            or len(set(artifact_ids)) != len(artifact_ids)
        ):
            raise ProviderLedgerError("IBKR finalized history interval is invalid")
        result[account_id] = {
            "calendar": "WEEKDAY_SOURCE_CALENDAR",
            "effective_end": end.isoformat(),
            "effective_start": start.isoformat(),
            "source_artifact_ids": list(artifact_ids),
            "statement_count": statement_count,
        }
    if set(result) != known_accounts:
        raise ProviderLedgerError("IBKR finalized history account scope differs")
    return result


def _schwab_flow_coverage(
    catalog: Mapping[str, Any], known_accounts: set[str]
) -> dict[str, dict[str, Any]]:
    raw_coverage = catalog.get("coverage")
    if raw_coverage is None:
        return {}
    coverage = _object(raw_coverage, "schwab coverage")
    raw_history = coverage.get("transaction_history")
    if raw_history is None:
        return {}
    history = _object(raw_history, "schwab transaction history coverage")
    _strict(
        history,
        {
            "accounts",
            "complete_through",
            "requested_interval",
            "source_manifest_sha256",
            "terminal_observed_at",
        },
        "schwab transaction history coverage",
    )
    if history["requested_interval"] != "ALL_AVAILABLE_HISTORY":
        raise ProviderLedgerError("Schwab transaction history interval is unsupported")
    manifest_sha256 = _run_id(
        history["source_manifest_sha256"],
        "Schwab transaction history manifest sha256",
    )
    complete_through = _day(
        history["complete_through"], "Schwab transaction history complete_through"
    )
    try:
        terminal_observed_at = datetime.fromisoformat(
            _text(
                history["terminal_observed_at"],
                "Schwab transaction history observed_at",
            )
        )
    except ValueError as exc:
        raise ProviderLedgerError(
            "Schwab transaction history observed_at is invalid"
        ) from exc
    if (
        terminal_observed_at.tzinfo is None
        or terminal_observed_at.utcoffset() is None
        or terminal_observed_at.date() != complete_through
    ):
        raise ProviderLedgerError("Schwab transaction history terminal date differs")
    raw_accounts = _object(history["accounts"], "Schwab transaction history accounts")
    if not raw_accounts or not set(raw_accounts).issubset(known_accounts):
        raise ProviderLedgerError("Schwab transaction history account scope differs")
    result: dict[str, dict[str, Any]] = {}
    for account_id, raw_claim in raw_accounts.items():
        claim = _object(raw_claim, "Schwab transaction history account")
        _strict(
            claim,
            {
                "effective_end",
                "effective_start",
                "source_manifest_sha256",
                "terminal_observed_at",
                "transaction_count",
            },
            "Schwab transaction history account",
        )
        start = _day(claim["effective_start"], "Schwab flow coverage start")
        end = _day(claim["effective_end"], "Schwab flow coverage end")
        count = claim["transaction_count"]
        if (
            start > end
            or end != complete_through
            or claim["source_manifest_sha256"] != manifest_sha256
            or claim["terminal_observed_at"] != history["terminal_observed_at"]
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise ProviderLedgerError(
                "Schwab transaction history account claim differs"
            )
        result[account_id] = dict(claim)
    return result


def _admissions(
    projections: Mapping[str, Sequence[Mapping[str, Any]]],
    evidence_rows: Sequence[Mapping[str, Any]],
    authoritative_provider_ids: set[str] | None = None,
    authoritative_strategy_ids: set[str] | None = None,
    schwab_flow_coverage: Mapping[str, Mapping[str, Any]] | None = None,
    ibkr_flow_coverage: Mapping[str, Mapping[str, Any]] | None = None,
    quantbase_flow_coverage: Mapping[str, Mapping[str, Any]] | None = None,
    matrixport_flow_coverage: Mapping[str, Mapping[str, Any]] | None = None,
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
    provider_flow_coverage = {
        "ibkr": dict(ibkr_flow_coverage or {}),
        "matrixport": dict(matrixport_flow_coverage or {}),
        "quantbase": dict(quantbase_flow_coverage or {}),
        "schwab": dict(schwab_flow_coverage or {}),
    }
    covered_flow_accounts: set[tuple[str, str]] = set()
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
        coverage_claim = (
            provider_flow_coverage.get(provider_id, {}).get(account_id)
            if fact_type == "flows"
            else None
        )
        if coverage_claim is not None:
            coverage_start = _day(
                coverage_claim.get("effective_start"),
                f"{provider_id} flow coverage start",
            )
            coverage_end = _day(
                coverage_claim.get("effective_end"),
                f"{provider_id} flow coverage end",
            )
            admitted_start, admitted_end = start, end
            if provider_id == "ibkr":
                # Flex completeness describes report dates. A cash event can
                # precede its first statement; retain its original event date.
                report_dates = [
                    _day(
                        row.get("nav_recognition_date") or row.get("event_date"),
                        "IBKR flow report date",
                    )
                    for row in rows
                ]
                admitted_start, admitted_end = min(report_dates), max(report_dates)
            if admitted_start < coverage_start or admitted_end > coverage_end:
                messages = {
                    "ibkr": "IBKR flow falls outside finalized history coverage",
                    "matrixport": (
                        "Matrixport flow falls outside terminal history coverage"
                    ),
                    "quantbase": (
                        "Quantbase flow falls outside terminal history coverage"
                    ),
                    "schwab": "Schwab flow falls outside terminal history coverage",
                }
                raise ProviderLedgerError(messages[provider_id])
            start, end = coverage_start, coverage_end
            if mapping_status == "MAPPED":
                covered_flow_accounts.add((provider_id, account_id))
        coverage_key = (
            "terminal_history_coverage"
            if provider_id in {"matrixport", "quantbase", "schwab"}
            else "finalized_history_coverage"
        )
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
                            **(
                                {coverage_key: dict(coverage_claim)}
                                if coverage_claim is not None
                                else {}
                            ),
                        }
                    )
                ).hexdigest(),
            )
        )
    for provider_id, coverage in provider_flow_coverage.items():
        for account_id in sorted(
            set(coverage)
            - {
                covered_account
                for covered_provider, covered_account in covered_flow_accounts
                if covered_provider == provider_id
            }
        ):
            claim = coverage[account_id]
            coverage_key = (
                "terminal_history_coverage"
                if provider_id in {"matrixport", "quantbase", "schwab"}
                else "finalized_history_coverage"
            )
            admissions.append(
                ProviderFactAdmission(
                    provider_id=provider_id,
                    fact_type="flows",
                    evidence_layer=EvidenceLayer.ACCOUNT_LIVE,
                    disposition=(
                        AdmissionDisposition.AUTHORITATIVE
                        if provider_id in provider_authority
                        else AdmissionDisposition.REFERENCE_ONLY
                    ),
                    effective_start=_day(
                        claim.get("effective_start"),
                        f"{provider_id} flow coverage start",
                    ),
                    effective_end=_day(
                        claim.get("effective_end"),
                        f"{provider_id} flow coverage end",
                    ),
                    account_id=account_id,
                    account_reconciliation_sha256=hashlib.sha256(
                        canonical_json_bytes(
                            {
                                "account_id": account_id,
                                "fact_type": "flows",
                                "provider_id": provider_id,
                                "records": [],
                                coverage_key: dict(claim),
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
    if authority.get("run_observation") is None:
        generation = _object(
            source.get("current_generation"), f"{source_id}.generation"
        )
        if generation != {
            "as_of": manifest.as_of_date.isoformat(),
            "generation_id": identity.generation_id,
            "sha256": identity.manifest_sha256,
        }:
            raise ProviderLedgerError(
                f"{provider_id} generation differs from financial MVP authority"
            )
    else:
        _observed_provider_selection(
            _object(authority["run_observation"], "run observation"),
            provider_id,
            identity.to_dict()
            | {
                "root": _object(
                    _object(authority["run_observation"], "run observation")[
                        "providers"
                    ],
                    "run observation providers",
                )[provider_id]["root"]
            },
            _day(
                _object(authority["run_observation"], "run observation")[
                    "financial_cutoff"
                ],
                "run observation cutoff",
            ),
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


def _validate_current_census_provider_generations(
    authority: Mapping[str, Any],
    identities: Sequence[ProviderGenerationIdentity],
    manifests: Mapping[str, GenerationManifest],
) -> None:
    """Bind candidate inputs to the contract's direct provider-lane census.

    The accepted provider-ledger generation is retained history evidence.  It
    is not the authority for a new candidate's provider inputs; those inputs
    must match the frozen direct lanes in ``current_census`` independently.
    """

    contract = _object(authority.get("contract"), "financial MVP contract")
    census = _object(contract.get("current_census"), "current_census")
    lanes = _objects(census.get("provider_lanes"), "current_census.provider_lanes")
    expected: dict[str, dict[str, Any]] = {}
    for lane in lanes:
        provider_id = _text(
            lane.get("provider_id"), "current census provider_id"
        ).lower()
        generation = lane.get("generation")
        if generation is None:
            continue
        if provider_id in expected:
            raise ProviderLedgerError(
                f"current census provider generation is duplicated: {provider_id}"
            )
        expected[provider_id] = _object(
            generation, f"current census provider generation {provider_id}"
        )

    selected = {identity.provider_id: identity for identity in identities}
    if (
        set(expected) != set(_PROVIDER_CATALOGS)
        or len(selected) != len(identities)
        or set(selected) != set(expected)
        or set(manifests) != set(expected)
    ):
        raise ProviderLedgerError("current census provider generation set differs")

    for provider_id, generation in expected.items():
        identity = selected[provider_id]
        manifest = manifests[provider_id]
        if authority.get("run_observation") is not None:
            observation = _object(authority["run_observation"], "run observation")
            _observed_provider_selection(
                observation,
                provider_id,
                identity.to_dict()
                | {
                    "root": _object(
                        _object(observation["providers"], "run observation providers")[
                            provider_id
                        ],
                        f"{provider_id} observation",
                    )["root"]
                },
                _day(observation["financial_cutoff"], "run observation cutoff"),
            )
            continue
        if (
            identity.generation_id != generation.get("generation_id")
            or identity.manifest_sha256 != generation.get("manifest_sha256")
            or manifest.as_of_date.isoformat() != generation.get("as_of")
        ):
            raise ProviderLedgerError(
                f"{provider_id} generation differs from current census authority"
            )


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


def _exclusion_readiness_impact(
    policy: Mapping[str, Any], source: Mapping[str, Any], reason: str
) -> str:
    """Resolve the readiness impact the owner policy assigns to one exclusion.

    A REQUIRED data class that materialized nothing blocks the product.  A
    source item inside a provider bundle that no product fact references does
    not: it is still recorded and published as an exclusion, but it has no
    current product effect.  The policy may map each allowed reason to its own
    impact; with no map the single scalar that predates it still governs, so
    an unamended contract behaves exactly as before.
    """

    by_reason = policy.get("exclusion_readiness_impact_by_reason")
    if isinstance(by_reason, Mapping) and reason in by_reason:
        return _text(by_reason[reason], "exclusion readiness impact by reason")
    default = policy.get("exclusion_readiness_impact")
    if default is not None:
        return _text(default, "exclusion readiness impact")
    return (
        "REQUIRED_NOT_READY"
        if source.get("product_requirement") == "REQUIRED"
        else "NO_CURRENT_PRODUCT_EFFECT"
    )


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
            readiness_impact=_exclusion_readiness_impact(policy, source, reason),
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
    observed_receipt_providers: list[dict[str, Any]] = []
    run_observation = authority.get("run_observation")
    by_provider = {item.provider_id: item for item in identities}
    for provider_id in sorted(_PROVIDER_CATALOGS):
        identity = by_provider[provider_id]
        manifest = manifests[provider_id]
        source = _source_segment(authority, provider_id, identity, manifest)
        policy = _dataset_disposition_policy(source)
        provider_items = _provider_inventory(
            provider_id, manifest, catalogs[provider_id], source
        )
        observed_inventory_digest = _inventory_digest(provider_items)
        if (
            run_observation is None
            and observed_inventory_digest != policy["upstream_inventory_sha256"]
        ):
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
        policy_day = (
            _day(
                _object(run_observation, "run observation")["financial_cutoff"],
                "run observation cutoff",
            )
            if run_observation is not None
            else manifest.as_of_date
        )
        if policy_start > policy_day or (
            policy_end is not None and policy_end < policy_day
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
        # Source coverage is a capture-envelope fact.  The producer has already
        # applied the run observation cutoff to the materialized facts, whose
        # observed_* bounds are carried separately by each disposition.  Never
        # rewrite a later capture envelope into a reversed source interval.
        source_observed_end = manifest.coverage.observed_end
        inventory.extend(provider_items)
        provider_dispositions = [
            _dataset_disposition(
                item,
                source,
                policy,
                manifest.coverage.observed_start,
                source_observed_end,
                provider_facts[provider_id],
                provider_strategy_returns,
                str(authority["contract_semantic_sha256"]),
            )
            for item in provider_items
        ]
        admitted_count, admitted_digest = _admitted_item_set_digest(
            provider_dispositions
        )
        if run_observation is None and (
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
                if disposition.readiness_impact != _exclusion_readiness_impact(
                    policy, source, disposition.reason or ""
                ):
                    raise ProviderLedgerError(
                        f"{provider_id} dataset exclusion readiness differs"
                    )
        dispositions.extend(provider_dispositions)
        if run_observation is not None:
            selection = _object(
                _object(run_observation, "run observation")["providers"],
                "run observation providers",
            )[provider_id]
            observed_receipt_providers.append(
                {
                    "catalog_sha256": identity.catalog_sha256,
                    "finality_evidence": selection["finality_evidence"],
                    "finality_interpretation": _validate_observed_finality_evidence(
                        provider_id, selection
                    ),
                    "finalized_through": selection["finalized_through"],
                    "generation_as_of_date": manifest.as_of_date.isoformat(),
                    "generation_id": identity.generation_id,
                    "historical_admitted_item_count": policy["admitted_item_count"],
                    "historical_admitted_item_set_sha256": policy[
                        "admitted_item_set_sha256"
                    ],
                    "historical_inventory_sha256": policy["upstream_inventory_sha256"],
                    "manifest_sha256": identity.manifest_sha256,
                    "observed_admitted_item_count": admitted_count,
                    "observed_admitted_item_set_sha256": admitted_digest,
                    "observed_inventory_count": len(provider_items),
                    "observed_inventory_sha256": observed_inventory_digest,
                    "provider_id": provider_id,
                    "qualification": identity.qualification.value,
                }
            )
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
    if run_observation is not None:
        observation = _object(run_observation, "run observation")
        receipt["run_observation"] = {
            "captured_at_utc": observation["captured_at_utc"],
            "financial_cutoff": observation["financial_cutoff"],
            "path": authority["run_observation_path"],
            "policy_contract_sha256": authority["contract_raw_sha256"],
            "providers": observed_receipt_providers,
            "sha256": authority["run_observation_sha256"],
            "size_bytes": authority["run_observation_size_bytes"],
        }
    validate_admit_or_declare_receipt(
        receipt,
        product_authority=authority,
        source_segments=_object(authority["sources"], "financial MVP sources"),
        provider_generations={str(row["provider_id"]): row for row in providers},
    )
    return receipt, canonical_json_bytes(receipt)


@_catalog_decimal_context
def build_catalog(
    spec_path: Path,
) -> tuple[dict[str, Any], GenerationManifest, bytes, bytes]:
    """Build one deterministic provider-ledger catalog and manifest."""

    spec = _load_spec(spec_path)
    authority = _financial_mvp_authority(spec)
    as_of = _day(spec["as_of_date"], "spec.as_of_date")
    run_observation = authority.get("run_observation")
    fx_rates = (
        _portfolio_fx_rates(spec["portfolio_fx_rates"], as_of)
        if "portfolio_fx_rates" in spec
        else None
    )
    provenance, provenance_artifact = _provenance(spec)
    accounts, strategies, assignments = _assignment_index(
        provenance, _object(authority.get("contract"), "financial MVP contract")
    )
    identities: list[ProviderGenerationIdentity] = []
    provider_coverage: list[dict[str, Any]] = []
    provider_artifacts: list[SourceArtifact] = []
    if fx_rates is not None:
        provider_artifacts.extend(fx_rates[2])
    combined: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exceptions: list[dict[str, Any]] = []
    catalogs: dict[str, dict[str, Any]] = {}
    manifests: dict[str, GenerationManifest] = {}
    provider_roots: dict[str, Path] = {}
    provider_facts: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for provider_id in sorted(_PROVIDER_CATALOGS):
        provider_ref = _object(spec["providers"], "providers")[provider_id]
        identity, manifest, provider_catalog, artifacts, root = _provider(
            provider_id,
            provider_ref,
            as_of,
            run_observation=(
                _object(run_observation, "run observation")
                if run_observation is not None
                else None
            ),
        )
        identities.append(identity)
        catalogs[provider_id] = provider_catalog
        manifests[provider_id] = manifest
        provider_roots[provider_id] = root
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
            fx_rates=fx_rates if provider_id == "matrixport" else None,
            financial_cutoff=(
                min(
                    as_of,
                    _day(
                        _object(
                            _object(run_observation, "run observation")["providers"],
                            "run observation providers",
                        )[provider_id]["finalized_through"],
                        f"{provider_id} observation finality",
                    ),
                )
                if run_observation is not None
                else as_of
            ),
        )
        provider_facts[provider_id] = facts
        for fact_type, rows in facts.items():
            combined[fact_type].extend(rows)
        exceptions.extend(findings)

    authoritative_strategy_ids = {
        str(row.get("strategy_id"))
        for row in _objects(authority["contract"].get("strategies"), "strategies")
        if row.get("product_requirement") == "REQUIRED"
    }
    _complete_schwab_security_transfer_values(
        combined.get("flows", []),
        catalogs["schwab"],
        catalogs["fidelity"],
        manifests["fidelity"].generation_id,
        {
            account_id
            for account_id, account_assignments in assignments.items()
            if any(
                assignment.get("mapping_status") == "MAPPED"
                and assignment.get("strategy_id") in authoritative_strategy_ids
                for assignment in account_assignments
            )
        },
        financial_cutoff=(
            min(
                as_of,
                _day(
                    _object(run_observation, "run observation")["providers"]["schwab"][
                        "finalized_through"
                    ],
                    "schwab observation finality",
                ),
            )
            if run_observation is not None
            else as_of
        ),
    )
    _validate_current_census_provider_generations(authority, identities, manifests)

    strategy_return_rows, evidence_rows, strategy_artifacts = _strategy_inputs(
        spec["strategy_inputs"],
        provenance,
        as_of,
        _object(spec["strategy_provider_map"], "strategy_provider_map"),
        financial_cutoffs=(
            {
                provider_id: _day(selection["finalized_through"], "provider cutoff")
                for provider_id, selection in run_observation["providers"].items()
            }
            if run_observation is not None
            else None
        ),
        cutoff_findings=exceptions if run_observation is not None else None,
    )
    source_strategy_returns: Sequence[Mapping[str, Any]] = strategy_return_rows
    declared_nav_gaps = _declared_nav_gap_dates_by_provider(manifests)
    source_strategy_returns = _returns_within_declared_nav_gaps(
        source_strategy_returns,
        declared_nav_gaps,
    )
    provider_native_strategy_ids = _provider_native_account_return_strategy_ids(
        _object(authority.get("contract"), "financial MVP contract")
    )
    _validate_provider_native_strategy_evidence(
        evidence_rows,
        provider_native_strategy_ids,
        manifests,
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
        capital_anchors=combined.get("capital_anchors", []),
        declared_nav_gap_dates_by_provider=declared_nav_gaps,
    )
    strategy_returns: Sequence[Mapping[str, Any]] = _merge_account_returns(
        source_strategy_returns,
        broker_returns,
        strategy_names,
        provider_native_strategy_ids=provider_native_strategy_ids,
    )
    funded_boundaries = _funded_account_live_boundaries(
        _object(authority.get("contract"), "financial MVP contract")
    )
    eligible_source_strategy_returns = _account_live_returns_within_funded_boundaries(
        source_strategy_returns,
        funded_boundaries,
    )
    strategy_returns = _account_live_returns_within_funded_boundaries(
        strategy_returns,
        funded_boundaries,
    )
    superseded_retained_return_count = sum(
        row.get("evidence_layer") == "ACCOUNT_LIVE"
        and str(row.get("strategy_id")) in provider_native_strategy_ids
        for row in eligible_source_strategy_returns
    )
    promoted_provider_return_count = sum(
        row.get("evidence_layer") == "ACCOUNT_LIVE"
        and str(row.get("strategy_id")) in provider_native_strategy_ids
        for row in strategy_returns
    )
    retained_source_return_count = (
        len(eligible_source_strategy_returns) - superseded_retained_return_count
    )
    appended_broker_return_count = (
        len(strategy_returns)
        - retained_source_return_count
        - promoted_provider_return_count
    )
    overlap_checks = _strategy_return_overlap_checks(
        source_strategy_returns,
        broker_returns,
        provider_native_strategy_ids=provider_native_strategy_ids,
    )
    provider_strategy_returns = _portfolio123_provider_returns(
        _object(spec["providers"], "providers")["portfolio123"],
        manifests["portfolio123"],
        evidence_rows,
    )
    if run_observation is not None:
        provider_strategy_returns, cutoff_findings = _returns_within_financial_cutoff(
            provider_strategy_returns,
            "portfolio123",
            min(
                as_of,
                _day(
                    _object(
                        _object(run_observation, "run observation")["providers"],
                        "run observation providers",
                    )["portfolio123"]["finalized_through"],
                    "portfolio123 observation finality",
                ),
            ),
        )
        exceptions.extend(cutoff_findings)
    evidence_rows = _refresh_strategy_evidence(
        [*strategy_returns, *provider_strategy_returns],
        evidence_rows,
        strategy_names,
        {item.provider_id: item for item in identities},
        {provider_id: item.as_of_date for provider_id, item in manifests.items()},
    )
    admissions = _admissions(
        combined,
        evidence_rows,
        authoritative_provider_ids,
        authoritative_strategy_ids,
        schwab_flow_coverage=_schwab_flow_coverage(catalogs["schwab"], accounts),
        ibkr_flow_coverage=_ibkr_flow_coverage(
            catalogs["ibkr"],
            {
                _text(item.get("account_id"), "IBKR account_id")
                for item in _objects(catalogs["ibkr"].get("accounts"), "IBKR accounts")
            },
        ),
        matrixport_flow_coverage=_matrixport_flow_coverage(
            manifests["matrixport"],
            catalogs["matrixport"],
            provider_roots["matrixport"],
            {_CSV_FACT_ACCOUNT["matrixport"]},
        ),
        quantbase_flow_coverage=_quantbase_flow_coverage(
            manifests["quantbase"],
            catalogs["quantbase"],
            provider_roots["quantbase"],
            {_CSV_FACT_ACCOUNT["quantbase"]},
        ),
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
        "capital_anchors": combined.get("capital_anchors", []),
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
                "appended_broker_return_count": appended_broker_return_count,
                "check_id": "broker-nav-rollforward",
                "promoted_provider_return_count": promoted_provider_return_count,
                "status": "PASS",
                "superseded_retained_return_count": (superseded_retained_return_count),
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
    if fx_rates is not None:
        catalog["portfolio_fx_generation"] = {
            "generation_id": fx_rates[0],
            "selection": "OWNER_SELECTED_YAHOO_DAILY_CLOSE_FOR_MATRIXPORT",
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
