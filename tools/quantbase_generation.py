"""Plan, stage, publish, verify, and cut over the frozen Quantbase baseline."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
import csv
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Any, Final

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    read_current,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes


PLAN_SCHEMA_VERSION: Final = 1
CATALOG_SCHEMA_VERSION: Final = 1
ADAPTER_VERSION: Final = "0.1.0"
DEFAULT_FILE_COUNT: Final = 55
DEFAULT_RECORD_COUNT: Final = 14901
PLANNING_RECORD_COUNT: Final = 14981
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DECLARED_SCHEMA_NAME = re.compile(r"^quantbase-[a-z0-9-]{1,80}$")
_DECLARED_SCHEMA_VERSION = re.compile(r"^[0-9a-z][0-9a-z.-]{0,31}$")
_NATIVE_ARTIFACT_CONTRACTS: Final[tuple[dict[str, tuple[str, str]], ...]] = (
    {
        "har/quantbase_20260902_171237.har": (
            "quantbase-native-har",
            "legacy-unknown",
        ),
        "har_output/responses/0274_qbapi3.com_200_application_json.json": (
            "quantbase-account-activity-response",
            "legacy-unknown",
        ),
    },
    {
        "capture/account-activity-response.json": (
            "quantbase-account-activity-response",
            "1",
        ),
        "capture/account-activity-request-proof.json": (
            "quantbase-account-activity-request-proof",
            "1",
        ),
    },
)

_HEADERS: Final[dict[str, tuple[str, ...]]] = {
    "account_activity": (
        "account_id",
        "activity_type",
        "alpaca_activity_id",
        "amount",
        "amount_cents",
        "asset",
        "aum_fee_transaction",
        "broker_journal_id",
        "canceled_at",
        "completed_at",
        "cost_basis",
        "created_at",
        "created_to_cover_fees",
        "date",
        "description",
        "destination_account_id",
        "destination_transaction_id",
        "executed",
        "failure_reason",
        "fees",
        "filled",
        "filled_at",
        "fund",
        "fund_title",
        "id",
        "initiating_account_type",
        "is_debit",
        "is_rebalance",
        "notify_user",
        "order_id",
        "origin_account_id",
        "origin_transaction_id",
        "per_share_amount",
        "quantity",
        "status",
        "submitted_at",
        "symbol",
        "time",
        "transaction",
        "transaction_id",
        "type",
        "updated_at",
        "user",
    ),
    "accounts": (
        "id",
        "type",
        "available_balance",
        "available_balance_cents",
        "settled_balance",
        "settled_balance_cents",
        "restricted_to_liquidation",
        "funds",
    ),
    "dashboard_holdings": (
        "symbol",
        "quantity",
        "value",
        "latest_price",
        "percentage",
    ),
    "dashboard_pricehistory": ("time", "value", "cost_basis", "profit_loss"),
    "dashboard_wins": ("metric", "value"),
    "fund_details": ("column", "value"),
    "fund_histdata": ("value", "time"),
    "fund_rebalance": ("date", "symbol", "percentage"),
    "fund_stats": (
        "id",
        "title",
        "live",
        "description",
        "additional_details",
        "portfolio_risks",
        "created_at",
        "brokerage",
        "rebalance_time",
        "equity_experience_min",
        "crypto_experience_min",
        "options_experience_min",
        "bonds_experience_min",
        "leverage_experience_min",
        "quantbase_fee",
        "annual_returns",
        "max_drawdown",
        "sharpe_ratio",
        "volatility",
        "correlation",
        "correlation_market",
        "min_investment",
        "key_considerations",
        "qualitative_risk_score",
        "risk_score",
        "last_rebalance",
        "scenarios",
        "partner",
        "backtest_end_date",
    ),
    "key_value": ("column", "value"),
    "ssg_pricehistory": ("fund_title", "fund_id", "value", "time"),
    "volatis_rebalance": ("date", "symbol", "proportion"),
}
_REQUIRED_FILES: Final = frozenset(
    {
        "account_activity.csv",
        "accounts.csv",
        "dashboard_Total_holdings.csv",
        "dashboard_Total_pricehistory.csv",
        "dashboard_total_wins.csv",
        "projections_all_live_funds.csv",
        "ssg_all_funds_stats.csv",
        "user_profile.csv",
        "volatis_strategy_rebalance_history.csv",
        "volatis_strategy_stats.csv",
    }
)
_DATE_FIELDS: Final = frozenset(
    {
        "backtest_end_date",
        "completed_at",
        "created_at",
        "date",
        "filled_at",
        "last_rebalance",
        "submitted_at",
        "time",
        "updated_at",
    }
)


class QuantbaseGenerationError(ValueError):
    """The frozen Quantbase source or cutover contract failed closed."""


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QuantbaseGenerationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise QuantbaseGenerationError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuantbaseGenerationError(f"{name} must be nonempty text")
    return value.strip()


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QuantbaseGenerationError(f"{name} must be a nonnegative integer")
    return value


def _absolute_directory(value: object, name: str, *, existing: bool) -> Path:
    path = Path(_text(value, name))
    if not path.is_absolute() or path.is_symlink():
        raise QuantbaseGenerationError(f"{name} must be a real absolute directory")
    if existing and not path.is_dir():
        raise QuantbaseGenerationError(f"{name} must exist")
    return path.resolve(strict=existing)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _write_create_only(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise QuantbaseGenerationError(f"existing evidence differs: {path.name}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _family(name: str) -> str:
    if name == "account_activity.csv":
        return "account_activity"
    if name == "accounts.csv":
        return "accounts"
    if name.startswith("dashboard_") and name.endswith("_holdings.csv"):
        return "dashboard_holdings"
    if name.startswith("dashboard_") and name.endswith("_pricehistory.csv"):
        return "dashboard_pricehistory"
    if name == "dashboard_total_wins.csv":
        return "dashboard_wins"
    if name.startswith("fund_details_") and name.endswith(".csv"):
        return "fund_details"
    if name.startswith("fund_histdata_") and name.endswith(".csv"):
        return "fund_histdata"
    if name.startswith("fund_rebalance_") and name.endswith(".csv"):
        return "fund_rebalance"
    if name in {"projections_all_live_funds.csv", "ssg_all_funds_stats.csv"}:
        return "fund_stats"
    if name in {"partner_status.csv", "user_profile.csv", "volatis_strategy_stats.csv"}:
        return "key_value"
    if name.startswith("ssg_") and name.endswith("_pricehistory.csv"):
        return "ssg_pricehistory"
    if name == "volatis_strategy_rebalance_history.csv":
        return "volatis_rebalance"
    raise QuantbaseGenerationError(f"unclassified Quantbase dataset: {name}")


def _parsed_date(value: str) -> date | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
            timestamp = float(raw)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (OverflowError, OSError, ValueError):
        return None
    return parsed if 2000 <= parsed.year <= 2100 else None


def _csv_metadata(path: Path) -> dict[str, object]:
    family = _family(path.name)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = tuple(next(reader))
            if header != _HEADERS[family]:
                raise QuantbaseGenerationError(f"header differs: {path.name}")
            row_count = 0
            observed_dates: list[date] = []
            date_indexes = [
                index for index, column in enumerate(header) if column in _DATE_FIELDS
            ]
            for row in reader:
                if len(row) != len(header):
                    raise QuantbaseGenerationError(f"row width differs: {path.name}")
                row_count += 1
                for index in date_indexes:
                    parsed = _parsed_date(row[index])
                    if parsed is not None:
                        observed_dates.append(parsed)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise QuantbaseGenerationError(f"CSV is unreadable: {path.name}") from exc
    digest, size = hash_file(path)
    return {
        "bytes": size,
        "column_count": len(header),
        "family": family,
        "header_sha256": hashlib.sha256(
            json.dumps(list(header), ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest(),
        "observed_end": max(observed_dates).isoformat() if observed_dates else None,
        "observed_start": min(observed_dates).isoformat() if observed_dates else None,
        "relative_path": f"csv/{path.name}",
        "row_count": row_count,
        "sha256": digest,
    }


def _source_layout(source_root: Path) -> tuple[list[Path], Path]:
    allowed = {".hermes", "csv", "RUNBOOK_QUANTBASE.md"}
    children = {path.name for path in source_root.iterdir()}
    if children - allowed:
        raise QuantbaseGenerationError("legacy source contains undeclared paths")
    hermes = source_root / ".hermes"
    if hermes.exists():
        if hermes.is_symlink() or any(
            path.is_symlink() or path.is_file() for path in hermes.rglob("*")
        ):
            raise QuantbaseGenerationError("legacy .hermes contains persisted state")
    csv_root = source_root / "csv"
    runbook = source_root / "RUNBOOK_QUANTBASE.md"
    if csv_root.is_symlink() or not csv_root.is_dir():
        raise QuantbaseGenerationError("legacy CSV directory is missing or linked")
    if runbook.is_symlink() or not runbook.is_file():
        raise QuantbaseGenerationError("legacy runbook is missing or linked")
    csv_children = list(csv_root.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in csv_children):
        raise QuantbaseGenerationError(
            "legacy CSV directory contains a linked/non-file path"
        )
    files = sorted(csv_children, key=lambda path: path.name.casefold())
    if not _REQUIRED_FILES <= {path.name for path in files}:
        raise QuantbaseGenerationError("required Quantbase datasets are missing")
    return files, runbook


def create_plan(
    source_root: Path,
    data_root: Path,
    *,
    as_of_date: date,
    quarantine_plan_id: str,
    expected_file_count: int = DEFAULT_FILE_COUNT,
    expected_record_count: int = DEFAULT_RECORD_COUNT,
    planning_record_count: int = PLANNING_RECORD_COUNT,
    native_artifacts: Sequence[tuple[str, Path]] | None = None,
    native_contract: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, object]:
    """Create one content-addressed, private cutover plan."""

    source = _absolute_directory(str(source_root), "source_root", existing=True)
    destination = _absolute_directory(str(data_root), "data_root", existing=True)
    if _is_within(destination, source) or _is_within(source, destination):
        raise QuantbaseGenerationError("source and data roots must be disjoint")
    if _SHA256.fullmatch(quarantine_plan_id) is None:
        raise QuantbaseGenerationError("quarantine plan ID is invalid")
    csv_files, runbook = _source_layout(source)
    datasets = [_csv_metadata(path) for path in csv_files]
    actual_rows = sum(_integer(item["row_count"], "row_count") for item in datasets)
    if len(datasets) != expected_file_count:
        raise QuantbaseGenerationError("CSV file count differs from accepted baseline")
    if actual_rows != expected_record_count:
        raise QuantbaseGenerationError(
            "CSV record count differs from accepted baseline"
        )
    runbook_hash, runbook_size = hash_file(runbook)
    if native_contract is not None and native_artifacts is None:
        raise QuantbaseGenerationError("a native contract requires native artifacts")
    native_entries: list[dict[str, object]] = []
    if native_artifacts is not None:
        relative_paths: list[str] = []
        source_paths: list[Path] = []
        for relative, raw_source_path in native_artifacts:
            portable = PurePosixPath(relative)
            if portable.is_absolute() or any(
                part in {"", ".", ".."} for part in portable.parts
            ):
                raise QuantbaseGenerationError(
                    "native artifact relative path is unsafe"
                )
            relative_paths.append(portable.as_posix())
            source_path = Path(raw_source_path)
            if (
                not source_path.is_absolute()
                or source_path.is_symlink()
                or not source_path.is_file()
            ):
                raise QuantbaseGenerationError(
                    "native artifact source must be a real absolute file"
                )
            source_paths.append(source_path.resolve(strict=True))
        if len(relative_paths) != len(set(relative_paths)):
            raise QuantbaseGenerationError(
                "native artifact relative paths must be unique"
            )
        if len(source_paths) != len(set(source_paths)):
            raise QuantbaseGenerationError("native artifact sources must be unique")
        contract = _native_artifact_contract(relative_paths, declared=native_contract)
        for relative, source_path in sorted(
            zip(relative_paths, source_paths, strict=True), key=lambda item: item[0]
        ):
            digest, size = hash_file(source_path)
            native_entries.append(
                {
                    "bytes": size,
                    "relative_path": relative,
                    "schema_name": contract[relative][0],
                    "schema_version": contract[relative][1],
                    "sha256": digest,
                    "source_path": str(source_path),
                }
            )
    observed = [
        date.fromisoformat(str(value))
        for item in datasets
        for value in (item["observed_start"], item["observed_end"])
        if value is not None
    ]
    payload: dict[str, object] = {
        "as_of_date": as_of_date.isoformat(),
        "baseline": {
            "accepted_file_count": expected_file_count,
            "accepted_record_count": expected_record_count,
            "planning_record_count": planning_record_count,
            "planning_record_count_difference": expected_record_count
            - planning_record_count,
            "planning_record_count_disposition": (
                "STALE_PLANNING_TOTAL_SUPERSEDED_BY_HASH_BOUND_FILES"
            ),
        },
        "data_root": str(destination),
        "datasets": datasets,
        **(
            {"native_artifacts": native_entries} if native_artifacts is not None else {}
        ),
        **(
            {
                "native_contract": {
                    key: list(value) for key, value in sorted(native_contract.items())
                }
            }
            if native_contract is not None
            else {}
        ),
        "observed_end": max(observed).isoformat(),
        "observed_start": min(observed).isoformat(),
        "provider_id": "quantbase",
        "quarantine_plan_id": quarantine_plan_id,
        "runbook": {
            "bytes": runbook_size,
            "relative_path": "RUNBOOK_QUANTBASE.md",
            "sha256": runbook_hash,
        },
        "schema_version": PLAN_SCHEMA_VERSION,
        "source_revision": "LEGACY_UNKNOWN",
        "source_root": str(source),
    }
    plan_id = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    run_id = f"legacy-{as_of_date.strftime('%Y%m%d')}-{plan_id[:12]}"
    plan = {**payload, "plan_id": plan_id, "run_id": run_id}
    plan_path = destination / "cutovers" / "plans" / plan_id / "plan.json"
    _write_create_only(plan_path, canonical_json_bytes(plan))
    return {
        "csv_file_count": len(datasets),
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "record_count": actual_rows,
        "run_id": run_id,
        "state": "PLANNED",
    }


def _load_plan(plan_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuantbaseGenerationError("plan is not valid UTF-8 JSON") from exc
    plan = _object(value, "plan")
    plan_id = plan.pop("plan_id", None)
    run_id = plan.pop("run_id", None)
    if not isinstance(plan_id, str) or _SHA256.fullmatch(plan_id) is None:
        raise QuantbaseGenerationError("plan ID is invalid")
    if hashlib.sha256(canonical_json_bytes(plan)).hexdigest() != plan_id:
        raise QuantbaseGenerationError("plan identity differs")
    expected_run_id = (
        f"legacy-{str(plan.get('as_of_date', '')).replace('-', '')}-{plan_id[:12]}"
    )
    if run_id != expected_run_id or plan_path.parent.name != plan_id:
        raise QuantbaseGenerationError("plan run/directory identity differs")
    plan["plan_id"] = plan_id
    plan["run_id"] = run_id
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise QuantbaseGenerationError("plan schema version is unsupported")
    return plan


def _authority_paths(plan: Mapping[str, Any]) -> tuple[Path, Path]:
    data_root = _absolute_directory(plan["data_root"], "data_root", existing=True)
    run_root = data_root / "source-runs" / _text(plan["run_id"], "run_id")
    return run_root / ".staging", run_root / "artifacts"


def _entry_path(root: Path, relative: str) -> Path:
    portable = PurePosixPath(relative)
    if portable.is_absolute() or any(
        part in {"", ".", ".."} for part in portable.parts
    ):
        raise QuantbaseGenerationError("plan relative path is unsafe")
    path = root.joinpath(*portable.parts)
    if not _is_within(path.resolve(strict=False), root.resolve(strict=False)):
        raise QuantbaseGenerationError("plan relative path escapes its root")
    return path


def _io_path(path: Path) -> Path:
    if os.name == "nt" and path.is_absolute() and not str(path).startswith("\\\\?\\"):
        return Path(f"\\\\?\\{path}")
    return path


def _native_entries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    if "native_artifacts" not in plan:
        return []
    native = _objects(plan["native_artifacts"], "native_artifacts")
    relative_paths: list[str] = []
    source_paths: list[str] = []
    for item in native:
        relative = _text(item.get("relative_path"), "native relative_path")
        _entry_path(Path("C:/quantbase-native-validation"), relative)
        relative_paths.append(relative)
        source_path = Path(_text(item.get("source_path"), "native source_path"))
        if not source_path.is_absolute():
            raise QuantbaseGenerationError("native artifact source must be absolute")
        source_paths.append(os.path.normcase(os.path.abspath(source_path)))
        _text(item.get("sha256"), "native sha256")
        _integer(item.get("bytes"), "native bytes")
    if len(relative_paths) != len(set(relative_paths)):
        raise QuantbaseGenerationError("native artifact relative paths must be unique")
    if len(source_paths) != len(set(source_paths)):
        raise QuantbaseGenerationError("native artifact sources must be unique")
    contract = _native_artifact_contract(
        relative_paths, declared=_declared_contract(plan.get("native_contract"))
    )
    for item, relative in zip(native, relative_paths, strict=True):
        if (
            item.get("schema_name") != contract[relative][0]
            or item.get("schema_version") != contract[relative][1]
        ):
            raise QuantbaseGenerationError("native artifact contract differs")
    return native


def _declared_contract(value: object) -> dict[str, tuple[str, str]] | None:
    """Validate a manifest-declared native contract; None selects the legacy set."""

    if value is None:
        return None
    declared: dict[str, tuple[str, str]] = {}
    for key, pair in _object(value, "native_contract").items():
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise QuantbaseGenerationError("native contract entry is malformed")
        name, version = pair
        if (
            not isinstance(name, str)
            or _DECLARED_SCHEMA_NAME.fullmatch(name) is None
            or not isinstance(version, str)
            or _DECLARED_SCHEMA_VERSION.fullmatch(version) is None
        ):
            raise QuantbaseGenerationError("native contract schema is invalid")
        declared[key] = (name, version)
    if not declared:
        raise QuantbaseGenerationError("native contract must not be empty")
    return declared


def _native_artifact_contract(
    relative_paths: Sequence[str],
    *,
    declared: Mapping[str, tuple[str, str]] | None = None,
) -> Mapping[str, tuple[str, str]]:
    if declared is not None:
        if set(relative_paths) != set(declared):
            raise QuantbaseGenerationError(
                "native artifacts differ from the declared native contract"
            )
        return declared
    for contract in _NATIVE_ARTIFACT_CONTRACTS:
        if set(relative_paths) == set(contract):
            return contract
    raise QuantbaseGenerationError(
        "native artifacts must contain the exact retained HAR and response pair"
    )


def _expected_entries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = _objects(plan.get("datasets"), "datasets")
    runbook = _object(plan.get("runbook"), "runbook")
    return [*entries, runbook, *_native_entries(plan)]


def _legacy_entries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        *_objects(plan.get("datasets"), "datasets"),
        _object(plan.get("runbook"), "runbook"),
    ]


def _verify_native_sources(plan: Mapping[str, Any]) -> None:
    for item in _native_entries(plan):
        relative = _text(item.get("relative_path"), "native relative_path")
        source_path = Path(_text(item.get("source_path"), "native source_path"))
        if (
            not source_path.is_absolute()
            or source_path.is_symlink()
            or not source_path.is_file()
        ):
            raise QuantbaseGenerationError(
                "native artifact source must be a real absolute file"
            )
        source_path = source_path.resolve(strict=True)
        digest, size = hash_file(source_path)
        if digest != item.get("sha256") or size != item.get("bytes"):
            raise QuantbaseGenerationError(f"planned input differs: {relative}")


def _verify_tree(root: Path, plan: Mapping[str, Any], *, legacy: bool) -> None:
    if root.is_symlink() or not root.is_dir():
        raise QuantbaseGenerationError("input root is missing or linked")
    expected = _legacy_entries(plan) if legacy else _expected_entries(plan)
    expected_paths = {str(item["relative_path"]) for item in expected}
    traversal_root = _io_path(root.resolve())
    actual_paths = {
        path.relative_to(traversal_root).as_posix()
        for path in traversal_root.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        raise QuantbaseGenerationError("input inventory differs from the cutover plan")
    if legacy:
        _source_layout(root)
        _verify_native_sources(plan)
    for item in expected:
        relative = _text(item.get("relative_path"), "relative_path")
        path = _io_path(_entry_path(root, relative))
        if path.is_symlink() or not path.is_file():
            raise QuantbaseGenerationError("planned input is missing or linked")
        digest, size = hash_file(path)
        if digest != item.get("sha256") or size != item.get("bytes"):
            raise QuantbaseGenerationError(f"planned input differs: {relative}")
        if relative.startswith("csv/"):
            metadata = _csv_metadata(path)
            for key in (
                "column_count",
                "family",
                "header_sha256",
                "observed_end",
                "observed_start",
                "row_count",
            ):
                if metadata[key] != item.get(key):
                    raise QuantbaseGenerationError(f"CSV metadata differs: {relative}")


def _copy_to_staging(plan: Mapping[str, Any]) -> Path:
    source = _absolute_directory(plan["source_root"], "source_root", existing=True)
    _verify_tree(source, plan, legacy=True)
    staging, authority = _authority_paths(plan)
    if authority.exists():
        _verify_tree(authority, plan, legacy=False)
        return authority
    staging.mkdir(parents=True, exist_ok=True)
    for item in _expected_entries(plan):
        relative = _text(item["relative_path"], "relative_path")
        source_path = (
            Path(_text(item["source_path"], "native source_path"))
            if "source_path" in item
            else _entry_path(source, relative)
        )
        destination = _entry_path(staging, relative)
        io_destination = _io_path(destination)
        io_destination.parent.mkdir(parents=True, exist_ok=True)
        if io_destination.exists():
            digest, size = hash_file(io_destination)
            if digest != item["sha256"] or size != item["bytes"]:
                raise QuantbaseGenerationError("staging contains conflicting bytes")
        else:
            shutil.copy2(source_path, io_destination)
    _verify_tree(staging, plan, legacy=False)
    plan_path = (
        _absolute_directory(plan["data_root"], "data_root", existing=True)
        / "cutovers"
        / "plans"
        / _text(plan["plan_id"], "plan_id")
        / "stage-result.json"
    )
    _write_create_only(
        plan_path,
        canonical_json_bytes(
            {
                "artifact_count": len(_expected_entries(plan)),
                "plan_id": plan["plan_id"],
                "state": "COPIED_AND_HASH_VERIFIED",
            }
        ),
    )
    return staging


def build_generation(
    plan_path: Path, *, input_root: Path | None = None
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    """Rebuild the metadata-only generation from exact private inputs."""

    plan = _load_plan(plan_path)
    if input_root is None:
        source = Path(_text(plan["source_root"], "source_root"))
        _, authority = _authority_paths(plan)
        input_root = source if source.is_dir() else authority
    root = _absolute_directory(str(input_root), "input_root", existing=True)
    legacy = root == Path(_text(plan["source_root"], "source_root")).resolve()
    _verify_tree(root, plan, legacy=legacy)
    as_of = date.fromisoformat(_text(plan["as_of_date"], "as_of_date"))
    datasets = _objects(plan["datasets"], "datasets")
    family_counts = Counter(_text(item["family"], "family") for item in datasets)
    family_rows: Counter[str] = Counter()
    for item in datasets:
        family_rows[_text(item["family"], "family")] += _integer(
            item["row_count"], "row_count"
        )
    baseline = _object(plan["baseline"], "baseline")
    native_entries = _native_entries(plan)
    catalog: dict[str, object] = {
        "api_fallback": False,
        "as_of_date": as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "known_gaps": [],
            "observed_end": plan["observed_end"],
            "observed_start": plan["observed_start"],
            "out_of_boundary": [
                (
                    "No freshness claim is made after the "
                    f"{as_of.isoformat()} declared capture cutoff."
                    if "native_contract" in plan
                    else "No freshness claim is made after the 2026-07-29 legacy capture."
                ),
                "The producing source revision is unknown and remains LEGACY_UNKNOWN.",
                "The captured runbook contains stale narrative counts; hash-bound CSV files govern.",
                "Inherited wins fields cannot be independently recomputed from the available evidence.",
            ],
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "datasets": [
            {
                "bytes": item["bytes"],
                "column_count": item["column_count"],
                "family": item["family"],
                "header_sha256": item["header_sha256"],
                "relative_path": item["relative_path"],
                "row_count": item["row_count"],
                "sha256": item["sha256"],
            }
            for item in datasets
        ],
        "family_dataset_counts": dict(sorted(family_counts.items())),
        "family_record_counts": dict(sorted(family_rows.items())),
        "legacy_lineage": {
            "capture_transport": (
                "EXECUTED_READ_ONLY_REQUEST"
                if any(
                    item["relative_path"]
                    == "capture/account-activity-request-proof.json"
                    for item in native_entries
                )
                else "HAR_ONLY"
            ),
            "producing_revision": plan["source_revision"],
            "quarantine_plan_id": plan["quarantine_plan_id"],
            "run_id": plan["run_id"],
        },
        "planning_reconciliation": {
            "accepted_record_count": baseline["accepted_record_count"],
            "difference": baseline["planning_record_count_difference"],
            "disposition": baseline["planning_record_count_disposition"],
            "planning_record_count": baseline["planning_record_count"],
        },
        "provider_id": "quantbase",
        "reconciliation": {
            "dataset_count": len(datasets),
            "record_count": sum(
                _integer(item["row_count"], "row_count") for item in datasets
            ),
            "status": "PASS_EXACT_TO_HASH_BOUND_FILES",
            "unclassified_dataset_count": 0,
        },
    }
    catalog_bytes = canonical_json_bytes(catalog)
    source_artifacts = tuple(
        SourceArtifact(
            artifact_id=f"source/{item['relative_path']}",
            sha256=_text(item["sha256"], "sha256"),
            size_bytes=_integer(item["bytes"], "bytes"),
            as_of_date=as_of,
            schema_name=f"quantbase-{_text(item['family'], 'family')}",
            schema_version="legacy-unknown",
        )
        for item in datasets
    )
    native_artifacts = tuple(
        SourceArtifact(
            artifact_id=f"source/{item['relative_path']}",
            sha256=_text(item["sha256"], "native sha256"),
            size_bytes=_integer(item["bytes"], "native bytes"),
            as_of_date=as_of,
            schema_name=_text(item["schema_name"], "native schema_name"),
            schema_version=_text(item["schema_version"], "native schema_version"),
        )
        for item in native_entries
    )
    runbook = _object(plan["runbook"], "runbook")
    plan_hash, plan_size = hash_file(plan_path)
    artifacts = (
        *source_artifacts,
        *native_artifacts,
        SourceArtifact(
            artifact_id="source/RUNBOOK_QUANTBASE.md",
            sha256=_text(runbook["sha256"], "runbook.sha256"),
            size_bytes=_integer(runbook["bytes"], "runbook.bytes"),
            as_of_date=as_of,
            schema_name="quantbase-legacy-runbook",
            schema_version="legacy-unknown",
        ),
        SourceArtifact(
            artifact_id="provenance/cutover-plan.json",
            sha256=plan_hash,
            size_bytes=plan_size,
            as_of_date=as_of,
            schema_name="quantbase-cutover-plan",
            schema_version=str(PLAN_SCHEMA_VERSION),
        ),
        SourceArtifact(
            artifact_id="normalized/quantbase-generation.json",
            sha256=hashlib.sha256(catalog_bytes).hexdigest(),
            size_bytes=len(catalog_bytes),
            as_of_date=as_of,
            schema_name="quantbase-generation-catalog",
            schema_version=str(CATALOG_SCHEMA_VERSION),
        ),
    )
    manifest = GenerationManifest.build(
        as_of_date=as_of,
        adapter=AdapterIdentity("quant-engine-web-contract-harvester", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=(
                f"{len(datasets)} hash-bound Quantbase CSV datasets from the dated "
                "legacy HAR extraction"
            ),
            fact_types=tuple(sorted(family_counts)),
            observed_start=date.fromisoformat(
                _text(plan["observed_start"], "observed_start")
            ),
            observed_end=date.fromisoformat(
                _text(plan["observed_end"], "observed_end")
            ),
            known_gaps=(),
        ),
        artifacts=artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def stage_candidate(plan_path: Path) -> dict[str, object]:
    plan = _load_plan(plan_path)
    staging = _copy_to_staging(plan)
    catalog, manifest, catalog_bytes = build_generation(plan_path, input_root=staging)
    data_root = _absolute_directory(plan["data_root"], "data_root", existing=True)
    manifest_path = stage_generation(data_root, manifest)
    catalog_path = manifest_path.parent / "quantbase-generation.json"
    _write_create_only(catalog_path, catalog_bytes)
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "dataset_count": len(catalog["datasets"]),  # type: ignore[arg-type]
        "generation_id": manifest.generation_id,
        "published": False,
        "record_count": catalog["reconciliation"]["record_count"],  # type: ignore[index]
        "state": "STAGED_AND_VERIFIED",
    }


def publish_candidate(plan_path: Path, generation_id: str) -> dict[str, object]:
    plan = _load_plan(plan_path)
    staged = stage_candidate(plan_path)
    if staged["generation_id"] != generation_id:
        raise QuantbaseGenerationError(
            "rebuilt generation differs from accepted candidate"
        )
    staging, authority = _authority_paths(plan)
    if not authority.exists():
        authority.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(authority)
    _verify_tree(authority, plan, legacy=False)
    _, manifest, catalog_bytes = build_generation(plan_path, input_root=authority)
    if manifest.generation_id != generation_id:
        raise QuantbaseGenerationError(
            "authority generation differs after cutover staging"
        )
    data_root = _absolute_directory(plan["data_root"], "data_root", existing=True)
    pointer = publish_generation(data_root, generation_id)
    return {
        "authority_root": str(authority),
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "generation_id": pointer.generation_id,
        "published": True,
        "state": "PUBLISHED",
    }


def verify_published(plan_path: Path, generation_id: str) -> dict[str, object]:
    plan = _load_plan(plan_path)
    _, authority = _authority_paths(plan)
    _verify_tree(authority, plan, legacy=False)
    _, manifest, catalog_bytes = build_generation(plan_path, input_root=authority)
    if manifest.generation_id != generation_id:
        raise QuantbaseGenerationError("rebuilt authority differs")
    data_root = _absolute_directory(plan["data_root"], "data_root", existing=True)
    pointer = read_current(data_root)
    if pointer.generation_id != generation_id:
        raise QuantbaseGenerationError("CURRENT.json points to another generation")
    catalog_path = (
        data_root / "generations" / generation_id / "quantbase-generation.json"
    )
    if catalog_path.read_bytes() != catalog_bytes:
        raise QuantbaseGenerationError("published normalized catalog differs")
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "generation_id": generation_id,
        "state": "VERIFIED_PUBLISHED",
    }


def _verify_remaining_legacy_source(root: Path, plan: Mapping[str, Any]) -> None:
    """Verify a full or partially removed source during a bounded retry."""

    if root.is_symlink() or not root.is_dir():
        raise QuantbaseGenerationError("legacy source is missing or linked")
    allowed_directories = {".hermes", ".hermes/plans", "csv"}
    actual_directories = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()
    }
    if not actual_directories <= allowed_directories:
        raise QuantbaseGenerationError("legacy source contains undeclared directories")
    expected = {
        _text(item["relative_path"], "relative_path"): item
        for item in _legacy_entries(plan)
    }
    actual_files = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
    }
    if not set(actual_files) <= set(expected):
        raise QuantbaseGenerationError("legacy source contains undeclared files")
    for relative, path in actual_files.items():
        item = expected[relative]
        if path.is_symlink():
            raise QuantbaseGenerationError("remaining legacy input is linked")
        digest, size = hash_file(path)
        if digest != item["sha256"] or size != item["bytes"]:
            raise QuantbaseGenerationError(
                f"remaining legacy input differs: {relative}"
            )


def _remove_empty_directory(path: Path) -> None:
    if os.name == "nt":
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    path.rmdir()


def cutover_source(plan_path: Path, generation_id: str) -> dict[str, object]:
    """Remove only the exact accepted legacy source after publication proof."""

    plan = _load_plan(plan_path)
    verify_published(plan_path, generation_id)
    source = Path(_text(plan["source_root"], "source_root"))
    if not source.exists():
        result_path = plan_path.parent / "cutover-result.json"
        return _object(json.loads(result_path.read_text(encoding="utf-8")), "result")
    source = _absolute_directory(str(source), "source_root", existing=True)
    _verify_remaining_legacy_source(source, plan)
    for item in _legacy_entries(plan):
        path = _entry_path(source, _text(item["relative_path"], "relative_path"))
        if path.exists():
            path.unlink()
    csv_root = source / "csv"
    if csv_root.exists():
        _remove_empty_directory(csv_root)
    hermes = source / ".hermes"
    if hermes.exists():
        for directory in sorted(
            (path for path in hermes.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            _remove_empty_directory(directory)
        _remove_empty_directory(hermes)
    _remove_empty_directory(source)
    result = {
        "generation_id": generation_id,
        "original_source_removed": True,
        "plan_id": plan["plan_id"],
        "recoverable_from": str(_authority_paths(plan)[1]),
        "state": "SINGLE_AUTHORITY_CUTOVER_COMPLETE",
    }
    _write_create_only(
        plan_path.parent / "cutover-result.json", canonical_json_bytes(result)
    )
    return result


def _load_native_intake(
    path: Path | None,
) -> tuple[
    list[tuple[str, Path]] | None, dict[str, tuple[str, str]] | None, date | None
]:
    """Re-derive the native declaration from a staged and parsed HAR intake.

    The declaration is never read from a caller-authored file: every raw file is
    re-hashed against the intake record, the staged manifest and the parse report
    must agree with that record, and the schema contract is built here.
    """

    if path is None:
        return None, None, None
    root = _absolute_directory(str(path), "native intake", existing=True)

    def load(name: str) -> tuple[bytes, dict[str, Any]]:
        target = root / name
        if target.is_symlink() or not target.is_file():
            raise QuantbaseGenerationError(f"native intake lacks {name}")
        raw = target.read_bytes()
        try:
            return raw, _object(json.loads(raw.decode("utf-8")), name)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QuantbaseGenerationError(f"native intake {name} is invalid") from exc

    manifest_bytes, manifest = load("manifest.json")
    _, record = load("intake-manifest.json")
    _, report = load("parse-report.json")
    files = _objects(record.get("files"), "intake files")
    if record.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
        raise QuantbaseGenerationError("native intake manifest differs from its record")
    if (
        report.get("manifest_sha256") != record.get("manifest_sha256")
        or report.get("intake_id") != record.get("intake_id")
        or report.get("files") != record.get("files")
        or report.get("state") not in {"PARSED", "PARSED_WITH_FLAGS"}
    ):
        raise QuantbaseGenerationError("native intake parse report does not match")
    raw_root = root / "raw"
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise QuantbaseGenerationError("native intake raw directory is missing")
    names = [_text(item.get("name"), "intake file name") for item in files]
    if {item.name for item in raw_root.iterdir()} != set(names) or len(names) != len(
        set(names)
    ):
        raise QuantbaseGenerationError("native intake raw inventory differs")
    artifacts: list[tuple[str, Path]] = []
    contract: dict[str, tuple[str, str]] = {}
    for item in files:
        name = str(item["name"])
        raw_path = raw_root / name
        if raw_path.is_symlink() or hash_file(raw_path) != (
            item.get("sha256"),
            item.get("bytes"),
        ):
            raise QuantbaseGenerationError(f"native intake raw file differs: {name}")
        artifacts.append((f"har/{name}", raw_path))
        contract[f"har/{name}"] = ("quantbase-native-har", "1")
    artifacts.append(("capture/intake-manifest.json", root / "intake-manifest.json"))
    contract["capture/intake-manifest.json"] = ("quantbase-har-intake-manifest", "1")
    cutoff_text = _text(manifest.get("cutoff"), "intake cutoff")
    return artifacts, contract, date.fromisoformat(cutoff_text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("source_root", type=Path)
    plan.add_argument("data_root", type=Path)
    plan.add_argument("--as-of-date", type=date.fromisoformat, required=True)
    plan.add_argument("--quarantine-plan-id", required=True)
    plan.add_argument("--expected-file-count", type=int, default=DEFAULT_FILE_COUNT)
    plan.add_argument("--expected-record-count", type=int, default=DEFAULT_RECORD_COUNT)
    plan.add_argument(
        "--planning-record-count", type=int, default=PLANNING_RECORD_COUNT
    )
    plan.add_argument("--native-intake", type=Path)
    for command in ("build", "stage"):
        child = commands.add_parser(command)
        child.add_argument("plan", type=Path)
    for command in ("publish", "verify", "cutover"):
        child = commands.add_parser(command)
        child.add_argument("plan", type=Path)
        child.add_argument("generation_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            native_artifacts, native_contract, intake_cutoff = _load_native_intake(
                args.native_intake
            )
            if intake_cutoff is not None and args.as_of_date != intake_cutoff:
                raise QuantbaseGenerationError(
                    "as-of date must equal the intake manifest cutoff"
                )
            result = create_plan(
                args.source_root,
                args.data_root,
                as_of_date=args.as_of_date,
                quarantine_plan_id=args.quarantine_plan_id,
                expected_file_count=args.expected_file_count,
                expected_record_count=args.expected_record_count,
                planning_record_count=args.planning_record_count,
                native_artifacts=native_artifacts,
                native_contract=native_contract,
            )
        elif args.command == "build":
            catalog, manifest, catalog_bytes = build_generation(args.plan)
            result = {
                "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
                "dataset_count": len(catalog["datasets"]),  # type: ignore[arg-type]
                "generation_id": manifest.generation_id,
                "record_count": catalog["reconciliation"]["record_count"],  # type: ignore[index]
                "state": "BUILT_AND_VERIFIED",
            }
        elif args.command == "stage":
            result = stage_candidate(args.plan)
        elif args.command == "publish":
            result = publish_candidate(args.plan, args.generation_id)
        elif args.command == "verify":
            result = verify_published(args.plan, args.generation_id)
        else:
            result = cutover_source(args.plan, args.generation_id)
    except (OSError, QuantbaseGenerationError, ValueError) as exc:
        print(f"quantbase-generation: FAIL: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
