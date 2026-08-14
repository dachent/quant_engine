"""Deterministic stage-only Schwab history and current-state generation builder."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from quant_engine.core import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    SourceArtifact,
    VerificationStatus,
    hash_file,
    publish_generation,
    stage_generation,
)
from quant_engine.core.contracts import canonical_json_bytes
from quant_engine.portfolio_ledger import (
    MappingStatus,
    NavObservation,
    ObservedAmount,
    PositionSnapshot,
    Transaction,
)


SPEC_SCHEMA_VERSION = 2
SUPPORTED_SPEC_SCHEMA_VERSIONS = frozenset({1, 2})
CATALOG_SCHEMA_VERSION = 1
ADAPTER_VERSION = "0.3.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_ROLES = {
    "MANUAL_NAV_CSV",
    "MANUAL_NAV_CSV_ALL",
    "MANUAL_POSITION_CSV",
    "MANUAL_TRANSACTION_JSON",
    "MANUAL_TRANSACTION_JSON_ALL",
    "WCH_NAV_CSV",
    "WCH_TRANSACTION_JSON",
    "WORKBOOK_REFERENCE",
}
_AGGREGATE_ROLES = frozenset({"MANUAL_NAV_CSV_ALL", "MANUAL_TRANSACTION_JSON_ALL"})


class SchwabGenerationError(ValueError):
    """A Schwab source, mapping, or generation contract failed closed."""


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SchwabGenerationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SchwabGenerationError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchwabGenerationError(f"{name} must be nonempty text")
    return value.strip()


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchwabGenerationError(f"{name} must be an integer")
    return value


def _date(value: object, name: str) -> date:
    raw = _text(value, name)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise SchwabGenerationError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise SchwabGenerationError(f"{name} must be a canonical ISO date")
    return parsed


def _relative(value: object, name: str) -> str:
    raw = _text(value, name)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SchwabGenerationError(f"{name} must be a portable relative path")
    return raw


def _root(value: object, name: str) -> Path:
    path = Path(_text(value, name))
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise SchwabGenerationError(f"{name} must be a real absolute directory")
    return path


def _strict(value: Mapping[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise SchwabGenerationError(
            f"{name} keys differ: missing={sorted(keys - set(value))}, "
            f"unexpected={sorted(set(value) - keys)}"
        )


def _decimal(value: object, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None or (isinstance(value, str) and value.strip() in {"", "--", "N/A"}):
        if optional:
            return None
        raise SchwabGenerationError(f"{name} is missing")
    raw = str(value).strip().replace("$", "").replace(",", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = f"-{raw[1:-1]}"
    raw = raw.replace("%", "")
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise SchwabGenerationError(f"{name} is not decimal") from exc
    if not parsed.is_finite():
        raise SchwabGenerationError(f"{name} must be finite")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _observed_date(
    value: object, name: str, *, allow_restatement_suffix: bool = False
) -> date:
    raw = _text(value, name)
    # The WCH restatement suffix " as of MM/DD/YYYY" is legitimate ONLY on
    # provider transactionDate fields, never on declared window boundaries.
    if allow_restatement_suffix:
        as_of_match = re.match(
            r"(\d{1,2}/\d{1,2}/\d{4}) as of (\d{1,2}/\d{1,2}/\d{4})$", raw
        )
        if as_of_match:
            raw = as_of_match.group(1)
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        # Parse the first 10 chars (the date portion) and verify no
        # unrecognised trailing content remains.
        prefix = raw[:10]
        try:
            parsed = datetime.strptime(prefix, pattern).date()
        except ValueError:
            continue
        if raw != prefix:
            raise SchwabGenerationError(
                f"{name} has trailing content after the date: {raw!r}"
            )
        return parsed
    raise SchwabGenerationError(f"{name} is not a supported date")


def _record_id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:24]}"


def _security_id(symbol: str) -> str:
    return f"security-schwab-{hashlib.sha256(symbol.encode()).hexdigest()[:24]}"


def _load_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchwabGenerationError("Schwab spec is not valid UTF-8 JSON") from exc
    spec = _object(value, "spec")
    version = _integer(spec.get("schema_version"), "schema_version")
    if version not in SUPPORTED_SPEC_SCHEMA_VERSIONS:
        raise SchwabGenerationError("Schwab spec schema version is unsupported")
    keys = {"as_of_date", "baseline", "crosswalk", "roots", "schema_version", "sources"}
    if version == SPEC_SCHEMA_VERSION:
        keys.add("current_snapshot")
    _strict(spec, keys, "spec")
    return spec


def _roots(value: object) -> dict[str, Path]:
    roots = _object(value, "roots")
    if not roots:
        raise SchwabGenerationError("roots must not be empty")
    result: dict[str, Path] = {}
    for key, item in roots.items():
        if _PORTABLE_ID.fullmatch(key) is None:
            raise SchwabGenerationError("root ID is not portable")
        result[key] = _root(item, f"roots.{key}")
    return result


def _source_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or not path.is_file():
        raise SchwabGenerationError("configured source is missing or linked")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise SchwabGenerationError("configured source escapes its root") from exc
    return path


def _crosswalk(value: object) -> tuple[dict[str, str], tuple[str, ...]]:
    records = _objects(value, "crosswalk")
    if len(records) != 8:
        raise SchwabGenerationError(
            "accepted Schwab brokerage crosswalk requires 8 accounts"
        )
    external_to_internal: dict[str, str] = {}
    suffix_to_internal: dict[str, str] = {}
    for record in records:
        _strict(record, {"external_account_id", "internal_account_id"}, "crosswalk[]")
        external = _text(record["external_account_id"], "external_account_id")
        internal = _text(record["internal_account_id"], "internal_account_id")
        if re.fullmatch(r"account-[0-9a-f]{16}", internal) is None:
            raise SchwabGenerationError("internal account ID is invalid")
        if (
            external in external_to_internal
            or internal in external_to_internal.values()
        ):
            raise SchwabGenerationError("crosswalk is not one-to-one")
        suffix = external[-3:]
        if len(suffix) != 3 or not suffix.isdigit() or suffix in suffix_to_internal:
            raise SchwabGenerationError("external suffix is not unique")
        external_to_internal[external] = internal
        suffix_to_internal[suffix] = internal
    return external_to_internal, tuple(sorted(suffix_to_internal))


def _sources(
    value: object, roots: Mapping[str, Path], generation_as_of: date
) -> tuple[dict[str, list[tuple[str | None, Path]]], tuple[SourceArtifact, ...]]:
    grouped: dict[str, list[tuple[str | None, Path]]] = {role: [] for role in _ROLES}
    artifacts: list[SourceArtifact] = []
    identifiers: set[str] = set()
    for record in _objects(value, "sources"):
        allowed_keys = {
            "account_id",
            "artifact_id",
            "as_of_date",
            "expected_record_count",
            "expected_sha256",
            "expected_size_bytes",
            "relative_path",
            "role",
            "root_id",
            "schema_name",
            "schema_version",
        }
        if not set(record).issubset(allowed_keys):
            raise SchwabGenerationError(
                f"sources[] keys differ: unexpected={sorted(set(record) - allowed_keys)}"
            )
        required_keys = allowed_keys - {"expected_record_count"}
        if not required_keys.issubset(set(record)):
            raise SchwabGenerationError(
                f"sources[] keys differ: missing={sorted(required_keys - set(record))}"
            )
        artifact_id = _relative(record["artifact_id"], "artifact_id")
        if artifact_id in identifiers:
            raise SchwabGenerationError("artifact IDs must be unique")
        identifiers.add(artifact_id)
        role = _text(record["role"], "role")
        if role not in _ROLES:
            raise SchwabGenerationError("source role is unsupported")
        root_id = _text(record["root_id"], "root_id")
        if root_id not in roots:
            raise SchwabGenerationError("source root is unknown")
        relative = _relative(record["relative_path"], "relative_path")
        path = _source_path(roots[root_id], relative)
        actual_hash, actual_size = hash_file(path)
        expected_hash = _text(record["expected_sha256"], "expected_sha256")
        if _SHA256.fullmatch(expected_hash) is None or actual_hash != expected_hash:
            raise SchwabGenerationError("source hash differs")
        if actual_size != _integer(
            record["expected_size_bytes"], "expected_size_bytes"
        ):
            raise SchwabGenerationError("source size differs")
        source_as_of = _date(record["as_of_date"], "source.as_of_date")
        if source_as_of > generation_as_of:
            raise SchwabGenerationError("source is newer than generation")
        account_value = record["account_id"]
        account_id = (
            None if account_value is None else _text(account_value, "account_id")
        )
        _no_account_roles = {
            "MANUAL_POSITION_CSV",
            "WORKBOOK_REFERENCE",
        } | _AGGREGATE_ROLES
        requires_account = role not in _no_account_roles
        if requires_account != (account_id is not None):
            raise SchwabGenerationError("source account scope is invalid for role")
        grouped[role].append((account_id, path))
        artifacts.append(
            SourceArtifact(
                artifact_id=artifact_id,
                sha256=actual_hash,
                size_bytes=actual_size,
                as_of_date=source_as_of,
                schema_name=_text(record["schema_name"], "schema_name"),
                schema_version=_text(record["schema_version"], "schema_version"),
            )
        )
    expected: dict[str, int] = {
        "MANUAL_NAV_CSV": 8,
        "MANUAL_NAV_CSV_ALL": 1,
        "MANUAL_POSITION_CSV": 1,
        "MANUAL_TRANSACTION_JSON": 8,
        "MANUAL_TRANSACTION_JSON_ALL": 1,
        "WCH_NAV_CSV": 8,
        "WCH_TRANSACTION_JSON": 8,
        "WORKBOOK_REFERENCE": 1,
    }
    # Allow opting into either per-account or aggregate roles, not both
    # (at least one manual transaction role and one manual NAV role required)
    if grouped.get("MANUAL_TRANSACTION_JSON") and grouped.get(
        "MANUAL_TRANSACTION_JSON_ALL"
    ):
        raise SchwabGenerationError(
            "cannot use both per-account and aggregate manual transaction roles"
        )
    if grouped.get("MANUAL_NAV_CSV") and grouped.get("MANUAL_NAV_CSV_ALL"):
        raise SchwabGenerationError(
            "cannot use both per-account and aggregate manual NAV roles"
        )
    # Drop empty roles from expected comparison (mutual exclusion above)
    actual_counts = {key: len(value) for key, value in grouped.items() if value}
    expected_filtered = {k: v for k, v in expected.items() if k in actual_counts}
    # Aggregate roles replace their per-account counterparts
    if grouped.get("MANUAL_TRANSACTION_JSON_ALL"):
        expected_filtered.pop("MANUAL_TRANSACTION_JSON", None)
    if grouped.get("MANUAL_NAV_CSV_ALL"):
        expected_filtered.pop("MANUAL_NAV_CSV", None)
    if actual_counts != expected_filtered:
        raise SchwabGenerationError("accepted source-role inventory differs")
    account_scoped_roles = {
        role: {account_id for account_id, _ in items}
        for role, items in grouped.items()
        if items
        and role not in {"MANUAL_POSITION_CSV", "WORKBOOK_REFERENCE"} | _AGGREGATE_ROLES
    }
    if any(
        len(accounts) != 8 or None in accounts
        for accounts in account_scoped_roles.values()
    ):
        raise SchwabGenerationError("account-scoped roles must cover 8 unique accounts")
    if len({frozenset(accounts) for accounts in account_scoped_roles.values()}) != 1:
        raise SchwabGenerationError("account-scoped role coverage differs")
    return grouped, tuple(sorted(artifacts, key=lambda item: item.artifact_id))


def _json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchwabGenerationError("source JSON is invalid") from exc


def _aware_datetime(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SchwabGenerationError(f"{name} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchwabGenerationError(f"{name} must be timezone-aware")
    if parsed.isoformat() != raw:
        raise SchwabGenerationError(f"{name} must be a canonical ISO datetime")
    return parsed


def _amount(value: object, name: str) -> dict[str, object]:
    amount = _object(value, name)
    _strict(amount, {"currency", "status", "value"}, name)
    if amount["currency"] != "USD":
        raise SchwabGenerationError(f"{name}.currency must be USD")
    status = _text(amount["status"], f"{name}.status")
    raw_value = amount["value"]
    if status == "AVAILABLE":
        parsed = _decimal(raw_value, f"{name}.value")
        assert parsed is not None
        normalized = _decimal_text(parsed)
        if raw_value != normalized:
            raise SchwabGenerationError(f"{name}.value must be canonical decimal text")
        return {"currency": "USD", "status": status, "value": normalized}
    if status == "UNAVAILABLE" and raw_value is None:
        return {"currency": "USD", "status": status, "value": None}
    raise SchwabGenerationError(f"{name} availability contract is invalid")


def _current_snapshot(
    value: object,
    *,
    roots: Mapping[str, Path],
    generation_as_of: date,
    account_ids: set[str],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    datetime,
    int,
    str,
    int,
    int,
    SourceArtifact,
]:
    source = _object(value, "current_snapshot")
    _strict(
        source,
        {
            "artifact_id",
            "as_of_date",
            "expected_sha256",
            "expected_size_bytes",
            "relative_path",
            "root_id",
            "schema_name",
            "schema_version",
        },
        "current_snapshot",
    )
    root_id = _text(source["root_id"], "current_snapshot.root_id")
    if root_id not in roots:
        raise SchwabGenerationError("current snapshot root is unknown")
    path = _source_path(
        roots[root_id],
        _relative(source["relative_path"], "current_snapshot.relative_path"),
    )
    actual_hash, actual_size = hash_file(path)
    expected_hash = _text(source["expected_sha256"], "current_snapshot.expected_sha256")
    if _SHA256.fullmatch(expected_hash) is None or actual_hash != expected_hash:
        raise SchwabGenerationError("current snapshot hash differs")
    if actual_size != _integer(
        source["expected_size_bytes"], "current_snapshot.expected_size_bytes"
    ):
        raise SchwabGenerationError("current snapshot size differs")
    source_as_of = _date(source["as_of_date"], "current_snapshot.as_of_date")
    if source_as_of != generation_as_of:
        raise SchwabGenerationError("current snapshot date differs from generation")

    document = _object(_json(path), "current snapshot")
    snapshot_version = _integer(
        document.get("schema_version"), "current snapshot schema_version"
    )
    snapshot_keys = {
        "accounts",
        "observed_at",
        "positions",
        "provider",
        "schema_version",
        "sweep_vehicle_count",
    }
    if snapshot_version == 2:
        snapshot_keys.add("transport")
    _strict(document, snapshot_keys, "current snapshot")
    if document["provider"] != "SCHWAB" or snapshot_version not in {1, 2}:
        raise SchwabGenerationError("current snapshot identity is unsupported")
    transport = (
        "DEVELOPER_API"
        if snapshot_version == 1
        else _text(document["transport"], "current snapshot transport")
    )
    if transport not in {"DEVELOPER_API", "WEB_CONTRACT"}:
        raise SchwabGenerationError("current snapshot transport is unsupported")
    evidence_class = (
        "DEVELOPER_API_CURRENT" if transport == "DEVELOPER_API" else "WCH_CURRENT"
    )
    observed_at = _aware_datetime(document["observed_at"], "current observed_at")
    if observed_at.date() != generation_as_of:
        raise SchwabGenerationError("current observation date differs from generation")

    accounts: list[dict[str, object]] = []
    observed_accounts: set[str] = set()
    current_balance_available_count = 0
    for record in _objects(document["accounts"], "current accounts"):
        _strict(
            record,
            {"account_id", "cash_available", "liquidation_value", "observed_at"},
            "current accounts[]",
        )
        account_id = _text(record["account_id"], "current account_id")
        if account_id not in account_ids or account_id in observed_accounts:
            raise SchwabGenerationError("current account scope is invalid")
        if (
            _aware_datetime(record["observed_at"], "current account observed_at")
            != observed_at
        ):
            raise SchwabGenerationError("current account observation time differs")
        observed_accounts.add(account_id)
        cash_available = _amount(record["cash_available"], "cash_available")
        liquidation_value = _amount(record["liquidation_value"], "liquidation_value")
        current_balance_available_count += liquidation_value["status"] == "AVAILABLE"
        accounts.append(
            {
                "account_id": account_id,
                "cash_available": cash_available,
                "liquidation_value": liquidation_value,
                "observed_at": observed_at.isoformat(),
            }
        )
    if observed_accounts != account_ids:
        raise SchwabGenerationError("current account scope differs from crosswalk")

    positions: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    for record in _objects(document["positions"], "current positions"):
        _strict(
            record,
            {
                "account_id",
                "as_of_date",
                "average_price",
                "evidence_class",
                "mapping_status",
                "market_value",
                "observed_at",
                "quantity",
                "record_id",
                "security_id",
                "strategy_id",
                "symbol",
            },
            "current positions[]",
        )
        account_id = _text(record["account_id"], "position.account_id")
        if account_id not in account_ids:
            raise SchwabGenerationError("current position account is outside scope")
        symbol = _text(record["symbol"], "position.symbol")
        security_id = _text(record["security_id"], "position.security_id")
        identity = (account_id, security_id)
        expected_identity = {
            "account_id": account_id,
            "as_of_date": generation_as_of.isoformat(),
            "security_id": _security_id(symbol),
        }
        if (
            security_id != expected_identity["security_id"]
            or _text(record["record_id"], "position.record_id")
            != _record_id("position", expected_identity)
            or identity in identities
        ):
            raise SchwabGenerationError("current position identity is invalid")
        if (
            record["as_of_date"] != generation_as_of.isoformat()
            or record["evidence_class"] != evidence_class
            or record["mapping_status"] != "UNMAPPED"
            or record["strategy_id"] is not None
            or _aware_datetime(record["observed_at"], "position.observed_at")
            != observed_at
        ):
            raise SchwabGenerationError("current position contract differs")
        quantity = _decimal(record["quantity"], "position.quantity")
        assert quantity is not None
        average_price = _decimal(
            record["average_price"], "position.average_price", optional=True
        )
        identities.add(identity)
        positions.append(
            {
                "account_id": account_id,
                "as_of_date": generation_as_of.isoformat(),
                "average_price": _decimal_text(average_price),
                "evidence_class": evidence_class,
                "mapping_status": "UNMAPPED",
                "market_value": _amount(record["market_value"], "market_value"),
                "observed_at": observed_at.isoformat(),
                "quantity": _decimal_text(quantity),
                "record_id": _record_id("position", expected_identity),
                "security_id": security_id,
                "strategy_id": None,
                "symbol": symbol,
            }
        )
    sweep_vehicle_count = _integer(
        document["sweep_vehicle_count"], "sweep_vehicle_count"
    )
    if sweep_vehicle_count < 0:
        raise SchwabGenerationError("sweep vehicle count must not be negative")
    current_position_account_count = len(
        {str(item["account_id"]) for item in positions}
    )
    accounts.sort(key=lambda item: str(item["account_id"]))
    positions.sort(key=lambda item: (str(item["account_id"]), str(item["security_id"])))
    artifact = SourceArtifact(
        artifact_id=_relative(source["artifact_id"], "current_snapshot.artifact_id"),
        sha256=actual_hash,
        size_bytes=actual_size,
        as_of_date=source_as_of,
        schema_name=_text(source["schema_name"], "current_snapshot.schema_name"),
        schema_version=_text(
            source["schema_version"], "current_snapshot.schema_version"
        ),
    )
    return (
        accounts,
        positions,
        observed_at,
        sweep_vehicle_count,
        transport,
        current_balance_available_count,
        current_position_account_count,
        artifact,
    )


def _current_position_reconciliation(
    api_positions: Sequence[Mapping[str, object]],
    manual_positions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    api_by_identity = {
        (str(item["account_id"]), str(item["symbol"])): item for item in api_positions
    }
    manual_by_identity = {
        (str(item["account_id"]), str(item["symbol"])): item
        for item in manual_positions
    }
    shared = set(api_by_identity) & set(manual_by_identity)
    quantity_comparable = 0
    quantity_equal = 0
    value_comparable = 0
    value_equal = 0
    for identity in shared:
        api = api_by_identity[identity]
        manual = manual_by_identity[identity]
        manual_quantity = manual.get("quantity")
        if manual_quantity is not None:
            quantity_comparable += 1
            quantity_equal += api.get("quantity") == manual_quantity
        api_value = _object(api.get("market_value"), "api market_value")
        manual_value = _object(manual.get("market_value"), "manual market_value")
        if api_value.get("status") == manual_value.get("status") == "AVAILABLE":
            value_comparable += 1
            value_equal += api_value.get("value") == manual_value.get("value")
    exact = (
        set(api_by_identity) == set(manual_by_identity)
        and quantity_comparable == quantity_equal
        and value_comparable == value_equal
    )
    return {
        "api_only_count": len(set(api_by_identity) - set(manual_by_identity)),
        "manual_only_count": len(set(manual_by_identity) - set(api_by_identity)),
        "quantity_comparable_count": quantity_comparable,
        "quantity_equal_count": quantity_equal,
        "shared_position_count": len(shared),
        "status": "PASS_EXACT" if exact else "PASS_OBSERVATIONAL_BOUNDARY",
        "type": "CURRENT_POSITION_REFERENCE",
        "value_comparable_count": value_comparable,
        "value_equal_count": value_equal,
    }


def _transaction_key(record: Mapping[str, Any], *, manual: bool) -> tuple[str, ...]:
    names = (
        (
            "Date",
            "Action",
            "Symbol",
            "Description",
            "Quantity",
            "Price",
            "Fees & Comm",
            "Amount",
        )
        if manual
        else (
            "transactionDate",
            "action",
            "symbol",
            "description",
            "shareQuantity",
            "executionPrice",
            "feesAndCommission",
            "amount",
        )
    )
    values: list[str] = []
    for index, name in enumerate(names):
        value = record.get(name)
        if index == 0:
            values.append(_observed_date(value, name).isoformat())
        elif index >= 4:
            values.append(_decimal_text(_decimal(value, name, optional=True)) or "")
        else:
            values.append("" if value is None else str(value).strip())
    return tuple(values)


def _transactions(
    wch: Sequence[tuple[str | None, Path]], manual: Sequence[tuple[str | None, Path]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    manual_by_account: dict[str, Counter[tuple[str, ...]]] = {}
    for account_id, path in manual:
        assert account_id is not None
        value = _object(_json(path), "manual transaction export")
        rows = value.get("BrokerageTransactions")
        if not isinstance(rows, list):
            raise SchwabGenerationError("manual transaction rows are missing")
        manual_by_account[account_id] = Counter(
            _transaction_key(_object(item, "manual transaction"), manual=True)
            for item in rows
        )
    normalized: list[dict[str, object]] = []
    reconciliations: list[dict[str, object]] = []
    for account_id, path in wch:
        assert account_id is not None
        raw_value = _json(path)
        if not isinstance(raw_value, list):
            raise SchwabGenerationError("WCH transaction source must be an array")
        keys: Counter[tuple[str, ...]] = Counter()
        for source_ordinal, item in enumerate(raw_value, start=1):
            record = _object(item, "WCH transaction")
            key = _transaction_key(record, manual=False)
            keys[key] += 1
            detail = record.get("transactionDetail")
            details = {} if detail is None else _object(detail, "transactionDetail")
            payload = {
                "account_id": account_id,
                "action": key[1],
                "amount": key[7] or None,
                "description": key[3],
                "effective_date": (
                    _observed_date(record["effectiveDate"], "effectiveDate").isoformat()
                    if record.get("effectiveDate")
                    else None
                ),
                "evidence_class": "WCH_HISTORICAL",
                "execution_price": key[5] or None,
                "fees_and_commission": key[6] or None,
                "quantity": key[4] or None,
                "settle_date": (
                    _observed_date(details["settleDate"], "settleDate").isoformat()
                    if details.get("settleDate")
                    else None
                ),
                "symbol": key[2] or None,
                "transaction_date": key[0],
                "source_ordinal": source_ordinal,
            }
            record_id = _record_id("txn", payload)
            observed_amount = _decimal(record.get("amount"), "amount", optional=True)
            symbol = key[2]
            ledger = Transaction(
                record_id=record_id,
                account_id=account_id,
                event_date=_observed_date(
                    record["transactionDate"],
                    "transactionDate",
                    allow_restatement_suffix=True,
                ),
                kind=key[1],
                amount=(
                    ObservedAmount.available(observed_amount, "USD")
                    if observed_amount is not None
                    else ObservedAmount.unavailable("USD")
                ),
                mapping_status=MappingStatus.UNMAPPED,
                security_id=_security_id(symbol) if symbol else None,
            ).to_dict()
            ledger.update(
                {
                    "description": key[3],
                    "effective_date": payload["effective_date"],
                    "evidence_class": "WCH_HISTORICAL",
                    "execution_price": key[5] or None,
                    "fees_and_commission": key[6] or None,
                    "quantity": key[4] or None,
                    "settle_date": payload["settle_date"],
                    "source_ordinal": source_ordinal,
                    "symbol": symbol or None,
                }
            )
            normalized.append(ledger)
        expected = manual_by_account.get(account_id)
        if expected is None or keys != expected:
            raise SchwabGenerationError("WCH and manual transaction histories differ")
        reconciliations.append(
            {
                "account_id": account_id,
                "fact_type": "TRANSACTION",
                "manual_count": sum(expected.values()),
                "source_count": sum(keys.values()),
                "status": "PASS_EXACT",
            }
        )
    normalized.sort(
        key=lambda item: (
            str(item["account_id"]),
            str(item["event_date"]),
            str(item["record_id"]),
        )
    )
    reconciliations.sort(key=lambda item: str(item["account_id"]))
    return normalized, reconciliations


def _transactions_all(
    wch: Sequence[tuple[str | None, Path]],
    manual_path: Path,
    *,
    expected_record_count: int | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object] | None]:
    """Reconcile an all-accounts manual transaction export against per-account WCH.

    The manual JSON has a declared window (FromDate/ToDate). Every manual record
    must content-match a WCH record within the window (subset-exact verification).
    Manual-only records → FAIL. WCH-only records → declared boundary gap
    (provider limitation). Returns a gap dict when WCH has uncovered in-window
    records.

    If expected_record_count is declared, the export must have at least that
    many rows (capture loss detection).
    """
    value = _object(_json(manual_path), "all-accounts manual transaction export")
    from_date_raw = _text(value.get("FromDate"), "FromDate")
    to_date_raw = _text(value.get("ToDate"), "ToDate")
    window_start = _observed_date(from_date_raw, "FromDate")
    window_end = _observed_date(to_date_raw, "ToDate")
    if window_start > window_end:
        raise SchwabGenerationError(
            f"transaction export window is reversed: FromDate={from_date_raw}, "
            f"ToDate={to_date_raw}"
        )
    rows = value.get("BrokerageTransactions")
    if not isinstance(rows, list):
        raise SchwabGenerationError("all-accounts manual transaction rows are missing")
    if expected_record_count is not None and len(rows) < expected_record_count:
        raise SchwabGenerationError(
            f"all-accounts manual export has {len(rows)} rows, "
            f"fewer than the declared expected {expected_record_count}"
        )
    manual_keys: Counter[tuple[str, ...]] = Counter()
    for item in rows:
        record = _object(item, "all-accounts manual transaction")
        key = _transaction_key(record, manual=True)
        manual_keys[key] += 1

    # Collect WCH transactions: all are normalized, only in-window are reconciled
    wch_keys: Counter[tuple[str, ...]] = Counter()
    normalized: list[dict[str, object]] = []
    for account_id, path in wch:
        assert account_id is not None
        raw_value = _json(path)
        if not isinstance(raw_value, list):
            raise SchwabGenerationError("WCH transaction source must be an array")
        for source_ordinal, item in enumerate(raw_value, start=1):
            record = _object(item, "WCH transaction")
            txn_date = _observed_date(
                record["transactionDate"],
                "transactionDate",
                allow_restatement_suffix=True,
            )
            key = _transaction_key(record, manual=False)
            in_window = window_start <= txn_date <= window_end
            if in_window:
                wch_keys[key] += 1
            detail = record.get("transactionDetail")
            details = {} if detail is None else _object(detail, "transactionDetail")
            payload = {
                "account_id": account_id,
                "action": key[1],
                "amount": key[7] or None,
                "description": key[3],
                "effective_date": (
                    _observed_date(record["effectiveDate"], "effectiveDate").isoformat()
                    if record.get("effectiveDate")
                    else None
                ),
                "evidence_class": "WCH_HISTORICAL",
                "execution_price": key[5] or None,
                "fees_and_commission": key[6] or None,
                "quantity": key[4] or None,
                "settle_date": (
                    _observed_date(details["settleDate"], "settleDate").isoformat()
                    if details.get("settleDate")
                    else None
                ),
                "symbol": key[2] or None,
                "transaction_date": key[0],
                "source_ordinal": source_ordinal,
            }
            record_id = _record_id("txn", payload)
            observed_amount = _decimal(record.get("amount"), "amount", optional=True)
            symbol = key[2]
            ledger = Transaction(
                record_id=record_id,
                account_id=account_id,
                event_date=_observed_date(
                    record["transactionDate"],
                    "transactionDate",
                    allow_restatement_suffix=True,
                ),
                kind=key[1],
                amount=(
                    ObservedAmount.available(observed_amount, "USD")
                    if observed_amount is not None
                    else ObservedAmount.unavailable("USD")
                ),
                mapping_status=MappingStatus.UNMAPPED,
                security_id=_security_id(symbol) if symbol else None,
            ).to_dict()
            ledger.update(
                {
                    "description": key[3],
                    "effective_date": payload["effective_date"],
                    "evidence_class": "WCH_HISTORICAL",
                    "execution_price": key[5] or None,
                    "fees_and_commission": key[6] or None,
                    "quantity": key[4] or None,
                    "settle_date": payload["settle_date"],
                    "source_ordinal": source_ordinal,
                    "symbol": symbol or None,
                }
            )
            normalized.append(ledger)

    extra_manual = manual_keys - wch_keys
    if extra_manual:
        sample = sorted(extra_manual)[:3]
        raise SchwabGenerationError(
            f"all-accounts manual has records not found in WCH within the declared window "
            f"({len(extra_manual)} manual-only records): {sample}"
        )
    # Verify shared keys have matching counts (manual must not exceed WCH)
    count_mismatches: list[tuple[str, ...]] = []
    for key in set(manual_keys) & set(wch_keys):
        if manual_keys[key] > wch_keys[key]:
            count_mismatches.append(key)
    if count_mismatches:
        sample = sorted(count_mismatches)[:3]
        raise SchwabGenerationError(
            f"all-accounts manual has higher count than WCH for shared keys "
            f"({len(count_mismatches)} affected): {sample}"
        )

    extra_wch = wch_keys - manual_keys
    wch_only_count = sum(extra_wch.values())
    status = "PASS_EXACT"
    gap: dict[str, object] | None = None
    if wch_only_count:
        # Declared boundary gap: Schwab's all-accounts export is structurally
        # partial (provider limitation + owner ruling #283, 2026-08-14).
        gap = {
            "export_count": sum(manual_keys.values()),
            "reason": "provider export is structurally partial (owner ruling #283)",
            "wch_in_window_count": sum(wch_keys.values()),
            "window_end": window_end.isoformat(),
            "window_start": window_start.isoformat(),
        }

    reconciliations: list[dict[str, object]] = [
        {
            "account_id": None,
            "fact_type": "TRANSACTION",
            "manual_count": sum(manual_keys.values()),
            "source_count": sum(wch_keys.values()),
            "status": status,
            "window_end": window_end.isoformat(),
            "window_start": window_start.isoformat(),
        }
    ]
    normalized.sort(
        key=lambda item: (
            str(item["account_id"]),
            str(item["event_date"]),
            str(item["record_id"]),
        )
    )
    return normalized, reconciliations, gap


def _nav_rows(path: Path, *, manual: bool) -> dict[date, Decimal]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            expected = (
                ["Date", "Amount"]
                if manual
                else ["account_id", "account_name", "date", "account_value"]
            )
            if reader.fieldnames != expected:
                raise SchwabGenerationError("NAV CSV columns differ")
            result: dict[date, Decimal] = {}
            for row in reader:
                observed = _observed_date(row["Date" if manual else "date"], "NAV date")
                amount = _decimal(
                    row["Amount" if manual else "account_value"], "NAV value"
                )
                assert amount is not None
                if observed in result and result[observed] != amount:
                    raise SchwabGenerationError("NAV source has a conflicting date")
                result[observed] = amount
            return result
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise SchwabGenerationError("NAV CSV is invalid") from exc


def _nav(
    wch: Sequence[tuple[str | None, Path]], manual: Sequence[tuple[str | None, Path]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    manual_by_account = {
        account_id: _nav_rows(path, manual=True)
        for account_id, path in manual
        if account_id is not None
    }
    normalized: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    for account_id, path in wch:
        assert account_id is not None
        observed = _nav_rows(path, manual=False)
        reference = manual_by_account.get(account_id)
        if reference is None:
            raise SchwabGenerationError("manual NAV reference is missing")
        common = set(observed).intersection(reference)
        differing = sorted(day for day in common if observed[day] != reference[day])
        source_only = sorted(set(observed) - set(reference))
        manual_only = sorted(set(reference) - set(observed))
        if source_only or len(manual_only) != 1:
            raise SchwabGenerationError(
                "NAV date boundary differs from the reviewed shape"
            )
        findings.append(
            {
                "account_id": account_id,
                "different_shared_dates": len(differing),
                "fact_type": "NAV",
                "manual_only_dates": len(manual_only),
                "shared_dates": len(common),
                "status": "PARTIAL_RESTATEMENT_BOUNDARY"
                if differing
                else "PASS_WITH_LATER_MANUAL_DATE",
            }
        )
        for observed_date, amount in sorted(observed.items()):
            identity = {
                "account_id": account_id,
                "as_of_date": observed_date.isoformat(),
            }
            payload = NavObservation(
                record_id=_record_id("nav", identity),
                account_id=account_id,
                as_of_date=observed_date,
                nav=ObservedAmount.available(amount, "USD"),
            ).to_dict()
            payload["evidence_class"] = "WCH_HISTORICAL"
            normalized.append(payload)
    normalized.sort(key=lambda item: (str(item["account_id"]), str(item["as_of_date"])))
    findings.sort(key=lambda item: str(item["account_id"]))
    return normalized, findings


def _nav_all(
    wch: Sequence[tuple[str | None, Path]],
    manual_path: Path,
    *,
    suffix_to_internal: Mapping[str, str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Reconcile an all-accounts manual balances CSV against per-account WCH NAV.

    The all-accounts CSV is a single-date multi-section report. Each section
    starts with an account label like "PCRA Trust XXXX-5620" where the last
    three digits map to a crosswalk suffix. The "Account Value" row within
    each section is compared against that account's WCH NAV for the same date.
    """
    try:
        with manual_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise SchwabGenerationError("all-accounts NAV CSV is invalid") from exc

    if not rows:
        raise SchwabGenerationError("all-accounts NAV CSV is empty")
    title = rows[0][0].strip() if rows[0] else ""
    match = re.search(r"as of (\d{2}/\d{2}/\d{4})", title)
    if match is None:
        raise SchwabGenerationError("all-accounts NAV title timestamp is missing")
    manual_date = datetime.strptime(match.group(1), "%m/%d/%Y").date()

    # Parse per-account sections from the CSV
    manual_by_account: dict[str, Decimal] = {}
    section_re = re.compile(r".*XXXX-(\d{4})$")
    current_account_id: str | None = None
    for row in rows:
        if not row or not row[0].strip():
            continue
        cell = row[0].strip().strip('"')
        sec_match = section_re.match(cell)
        if sec_match:
            suffix4 = sec_match.group(1)
            suffix3 = suffix4[-3:]
            current_account_id = suffix_to_internal.get(suffix3)
            if current_account_id is None:
                raise SchwabGenerationError(
                    f"all-accounts NAV section suffix '{suffix3}' is not in the crosswalk"
                )
            if current_account_id in manual_by_account:
                raise SchwabGenerationError(
                    f"all-accounts NAV has duplicate section for account "
                    f"{current_account_id}"
                )
            continue
        if current_account_id is not None and cell == "Account Value":
            raw = row[1].strip() if len(row) > 1 else ""
            amount = _decimal(raw, f"Account Value for {current_account_id}")
            assert amount is not None
            manual_by_account[current_account_id] = amount
            current_account_id = None

    expected_ids = set(suffix_to_internal.values())
    found_ids = set(manual_by_account)
    if found_ids != expected_ids:
        missing = sorted(expected_ids - found_ids)
        extra = sorted(found_ids - expected_ids)
        raise SchwabGenerationError(
            f"all-accounts NAV account coverage differs from crosswalk: "
            f"missing={missing}, extra={extra}"
        )

    # Compare each account's value against WCH NAV for the manual date
    wch_by_account: dict[str, dict[date, Decimal]] = {}
    for account_id, path in wch:
        assert account_id is not None
        wch_by_account[account_id] = _nav_rows(path, manual=False)

    per_account_findings: list[dict[str, object]] = []
    for account_id in sorted(expected_ids):
        wch_rows = wch_by_account[account_id]
        wch_value = wch_rows.get(manual_date)
        manual_value = manual_by_account[account_id]
        if wch_value is None:
            latest_wch = max(wch_rows.keys()) if wch_rows else None
            if latest_wch is None or manual_date <= latest_wch:
                raise SchwabGenerationError(
                    f"NAV date boundary is unexpected for account {account_id}"
                )
            per_account_findings.append(
                {
                    "account_id": account_id,
                    "fact_type": "NAV",
                    "manual_date": manual_date.isoformat(),
                    "status": "PASS_WITH_LATER_MANUAL_DATE",
                }
            )
        elif wch_value != manual_value:
            raise SchwabGenerationError(
                f"all-accounts NAV value differs for account {account_id} on "
                f"{manual_date.isoformat()}: manual={manual_value}, WCH={wch_value}"
            )
        else:
            per_account_findings.append(
                {
                    "account_id": account_id,
                    "fact_type": "NAV",
                    "manual_date": manual_date.isoformat(),
                    "status": "PASS_EXACT",
                }
            )

    normalized: list[dict[str, object]] = []
    for account_id, path in wch:
        assert account_id is not None
        observed = _nav_rows(path, manual=False)
        for obs_date, amount in sorted(observed.items()):
            identity = {
                "account_id": account_id,
                "as_of_date": obs_date.isoformat(),
            }
            payload = NavObservation(
                record_id=_record_id("nav", identity),
                account_id=account_id,
                as_of_date=obs_date,
                nav=ObservedAmount.available(amount, "USD"),
            ).to_dict()
            payload["evidence_class"] = "WCH_HISTORICAL"
            normalized.append(payload)

    per_account_findings.sort(key=lambda item: str(item["account_id"]))
    normalized.sort(key=lambda item: (str(item["account_id"]), str(item["as_of_date"])))
    return normalized, per_account_findings


def _positions(
    path: Path, suffix_to_internal: Mapping[str, str]
) -> tuple[list[dict[str, object]], str, int]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise SchwabGenerationError("position CSV is invalid") from exc
    if not rows or len(rows[0]) != 1:
        raise SchwabGenerationError("position export title is missing")
    title = rows[0][0].strip()
    match = re.search(r"as of (\d{1,2}:\d{2} [AP]M) ET, (\d{2}/\d{2}/\d{4})$", title)
    if match is None:
        raise SchwabGenerationError("position export timestamp is invalid")
    observed_date = datetime.strptime(match.group(2), "%m/%d/%Y").date()
    observed_time = datetime.strptime(match.group(1), "%I:%M %p").time()
    observed_at = datetime.combine(observed_date, observed_time).isoformat()
    normalized: list[dict[str, object]] = []
    unavailable_quantity_count = 0
    index = 1
    account_ids: set[str] = set()
    while index < len(rows):
        nonempty = [item.strip() for item in rows[index] if item.strip()]
        if not nonempty:
            index += 1
            continue
        if len(nonempty) != 1:
            raise SchwabGenerationError("position account section is invalid")
        suffix_match = re.search(r"\.\.\.(\d{3})$", nonempty[0])
        if suffix_match is None or suffix_match.group(1) not in suffix_to_internal:
            raise SchwabGenerationError("position account suffix is unmapped")
        account_id = suffix_to_internal[suffix_match.group(1)]
        if account_id in account_ids:
            raise SchwabGenerationError("position account section is duplicated")
        account_ids.add(account_id)
        index += 1
        if index >= len(rows):
            raise SchwabGenerationError("position header is missing")
        headers = [item.strip() for item in rows[index]]
        required = {
            "Symbol",
            "Description",
            "Qty (Quantity)",
            "Cost Basis",
            "Price",
            "Mkt Val (Market Value)",
            "Asset Type",
        }
        if not required.issubset(headers):
            raise SchwabGenerationError("position columns differ")
        index += 1
        while index < len(rows) and any(item.strip() for item in rows[index]):
            values = [item.strip() for item in rows[index]]
            values += [""] * (len(headers) - len(values))
            record = dict(zip(headers, values, strict=False))
            symbol = record["Symbol"]
            if symbol:
                quantity = _decimal(record["Qty (Quantity)"], "quantity", optional=True)
                market_value = _decimal(
                    record["Mkt Val (Market Value)"], "market_value", optional=True
                )
                identity = {
                    "account_id": account_id,
                    "as_of_date": observed_date.isoformat(),
                    "security_id": _security_id(symbol),
                    "symbol": symbol,
                }
                observed_market_value = (
                    ObservedAmount.available(market_value, "USD")
                    if market_value is not None
                    else ObservedAmount.unavailable("USD")
                )
                position_payload: dict[str, object]
                if quantity is None:
                    unavailable_quantity_count += 1
                    position_payload = {
                        "account_id": account_id,
                        "as_of_date": observed_date.isoformat(),
                        "mapping_status": MappingStatus.UNMAPPED.value,
                        "market_value": observed_market_value.to_dict(),
                        "quantity": None,
                        "record_id": _record_id("position", identity),
                        "security_id": _security_id(symbol),
                        "strategy_id": None,
                    }
                else:
                    position_payload = PositionSnapshot(
                        record_id=_record_id("position", identity),
                        account_id=account_id,
                        security_id=_security_id(symbol),
                        as_of_date=observed_date,
                        quantity=quantity,
                        market_value=observed_market_value,
                        mapping_status=MappingStatus.UNMAPPED,
                    ).to_dict()
                position_payload.update(
                    {
                        "as_of_local_datetime": observed_at,
                        "asset_type": record["Asset Type"] or None,
                        "cost_basis": _decimal_text(
                            _decimal(record["Cost Basis"], "cost_basis", optional=True)
                        ),
                        "description": record["Description"] or None,
                        "evidence_class": "MANUAL_INTRADAY_POSITION",
                        "price": _decimal_text(
                            _decimal(record["Price"], "price", optional=True)
                        ),
                        "quantity_status": (
                            "AVAILABLE" if quantity is not None else "UNAVAILABLE"
                        ),
                        "symbol": symbol,
                        "time_zone": "America/New_York",
                    }
                )
                normalized.append(position_payload)
            index += 1
    if len(account_ids) != 8:
        raise SchwabGenerationError(
            "position export does not cover 8 brokerage accounts"
        )
    normalized.sort(key=lambda item: (str(item["account_id"]), str(item["symbol"])))
    return normalized, observed_at, unavailable_quantity_count


def build_generation(
    spec_path: Path,
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    """Verify all configured sources and build a deterministic partial generation."""

    spec = _load_spec(spec_path)
    generation_as_of = _date(spec["as_of_date"], "as_of_date")
    roots = _roots(spec["roots"])
    crosswalk, suffixes = _crosswalk(spec["crosswalk"])
    suffix_to_internal = {
        external[-3:]: internal for external, internal in crosswalk.items()
    }
    if tuple(sorted(suffix_to_internal)) != suffixes:
        raise SchwabGenerationError("crosswalk suffix derivation differs")
    sources, artifacts = _sources(spec["sources"], roots, generation_as_of)
    txn_partial_gap: dict[str, object] | None = None
    expected_export_record_count: int | None = None
    if sources.get("MANUAL_TRANSACTION_JSON_ALL"):
        # Read the declared export record count from the spec source entry
        manual_all_sources = [
            s for s in spec["sources"] if s.get("role") == "MANUAL_TRANSACTION_JSON_ALL"
        ]
        if manual_all_sources and "expected_record_count" in manual_all_sources[0]:
            expected_export_record_count = _integer(
                manual_all_sources[0]["expected_record_count"],
                "expected_record_count",
            )
        transactions, transaction_reconciliation, txn_partial_gap = _transactions_all(
            sources["WCH_TRANSACTION_JSON"],
            sources["MANUAL_TRANSACTION_JSON_ALL"][0][1],
            expected_record_count=expected_export_record_count,
        )
    else:
        transactions, transaction_reconciliation = _transactions(
            sources["WCH_TRANSACTION_JSON"], sources["MANUAL_TRANSACTION_JSON"]
        )
    if sources.get("MANUAL_NAV_CSV_ALL"):
        nav, nav_reconciliation = _nav_all(
            sources["WCH_NAV_CSV"],
            sources["MANUAL_NAV_CSV_ALL"][0][1],
            suffix_to_internal=suffix_to_internal,
        )
    else:
        nav, nav_reconciliation = _nav(
            sources["WCH_NAV_CSV"], sources["MANUAL_NAV_CSV"]
        )
    manual_positions, manual_position_as_of, unavailable_position_quantity = _positions(
        sources["MANUAL_POSITION_CSV"][0][1], suffix_to_internal
    )
    current_accounts: list[dict[str, object]] | None = None
    positions = manual_positions
    position_as_of: object = manual_position_as_of
    current_reconciliation: list[dict[str, object]] = []
    sweep_vehicle_count: int | None = None
    current_transport: str | None = None
    current_balance_available_count = 0
    current_position_account_count = 0
    if spec["schema_version"] == SPEC_SCHEMA_VERSION:
        (
            current_accounts,
            positions,
            current_observed_at,
            sweep_vehicle_count,
            current_transport,
            current_balance_available_count,
            current_position_account_count,
            current_artifact,
        ) = _current_snapshot(
            spec["current_snapshot"],
            roots=roots,
            generation_as_of=generation_as_of,
            account_ids=set(crosswalk.values()),
        )
        position_as_of = current_observed_at.isoformat()
        artifacts = tuple(
            sorted((*artifacts, current_artifact), key=lambda item: item.artifact_id)
        )
        current_reconciliation.append(
            _current_position_reconciliation(positions, manual_positions)
        )
    baseline = _object(spec["baseline"], "baseline")
    expected = {
        "account_count": len(set(crosswalk.values())),
        "nav_count": len(nav),
        "position_count": len(positions),
        "transaction_count": len(transactions),
    }
    for key, actual in expected.items():
        if _integer(baseline.get(key), f"baseline.{key}") != actual:
            raise SchwabGenerationError(f"{key} differs from accepted baseline")
    observed_dates = [_date(item["as_of_date"], "nav.as_of_date") for item in nav]
    known_gaps = (
        *(
            ("current account and position snapshot is not present",)
            if current_accounts is None
            else ()
        ),
        *(
            ("current account balances remain unavailable",)
            if current_accounts is not None
            and current_balance_available_count < len(current_accounts)
            else ()
        ),
        *(
            (
                f"{len(crosswalk) - current_position_account_count} accepted accounts have no reported non-cash position row",
            )
            if current_accounts is not None
            and current_position_account_count < len(crosswalk)
            else ()
        ),
        "historical NAV has one later manual date per brokerage account",
        "two account histories contain documented intraday restatements",
        "Investor Checking is outside the accepted brokerage boundary",
        *(
            (
                f"manual_export_partial:"
                f"{txn_partial_gap['window_start']}-{txn_partial_gap['window_end']}"
                f" export={txn_partial_gap['export_count']}"
                f" wch={txn_partial_gap['wch_in_window_count']}"
                f" ({txn_partial_gap['reason']})",
            )
            if txn_partial_gap is not None
            else ()
        ),
        *(
            (
                f"{unavailable_position_quantity} manual position rows have unavailable quantity",
            )
            if unavailable_position_quantity and current_accounts is None
            else ()
        ),
    )
    catalog: dict[str, object] = {
        "accounts": [
            {"account_id": item, "base_currency": "USD", "provider": "SCHWAB"}
            for item in sorted(set(crosswalk.values()))
        ],
        "as_of_date": generation_as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "known_gaps": list(known_gaps),
            "status": "PARTIAL",
        },
        "nav_observations": nav,
        "positions": positions,
        "position_snapshot_as_of": (
            {"observed_at": position_as_of}
            if current_accounts is not None
            else {
                "local_datetime": position_as_of,
                "time_zone": "America/New_York",
            }
        ),
        "position_quantity_unavailable_count": (
            0 if current_accounts is not None else unavailable_position_quantity
        ),
        "reconciliation": [
            *transaction_reconciliation,
            *nav_reconciliation,
            *current_reconciliation,
        ],
        "source_artifacts": [item.to_dict() for item in artifacts],
        "source_provenance": {
            "developer_repository_head": "4864bc1428563b79cdeda35d55dd5f7c73b4ee3c",
            "developer_repository_license": "NONE_DECLARED",
            "web_contract_package_version": "0.1.0",
        },
        "transactions": transactions,
    }
    if current_accounts is not None:
        catalog.update(
            {
                "current_accounts": current_accounts,
                "current_balance_available_count": current_balance_available_count,
                "current_position_account_count": current_position_account_count,
                "current_transport": current_transport,
                "manual_position_quantity_unavailable_count": unavailable_position_quantity,
                "sweep_vehicle_count": sweep_vehicle_count,
            }
        )
    catalog_bytes = canonical_json_bytes(catalog)
    normalized_artifact = SourceArtifact(
        artifact_id="normalized/schwab-generation.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=generation_as_of,
        schema_name="schwab-generation-catalog",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    manifest = GenerationManifest.build(
        as_of_date=generation_as_of,
        adapter=AdapterIdentity("quant-engine-schwab", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=(
                CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
                if current_accounts is not None
                else CoverageStatus.PARTIAL
            ),
            boundary=(
                f"8 mapped Schwab brokerage accounts from {current_transport}, WCH history, and manual reference evidence"
                + (
                    f" (export record count {expected_export_record_count} per owner ruling #283)"
                    if expected_export_record_count is not None
                    else ""
                )
                if current_transport is not None
                else "8 mapped Schwab brokerage accounts from reviewed WCH and manual evidence"
            ),
            fact_types=(
                (
                    (
                        "current_balances",
                        "historical_nav",
                        "positions",
                        "transactions",
                    )
                    if current_balance_available_count == len(current_accounts)
                    else ("historical_nav", "positions", "transactions")
                )
                if current_accounts is not None
                else ("historical_nav", "positions", "transactions")
            ),
            observed_start=min(observed_dates),
            observed_end=(
                max((*observed_dates, generation_as_of))
                if current_accounts is not None
                else max(observed_dates)
            ),
            known_gaps=known_gaps if current_accounts is None else (),
        ),
        artifacts=(*artifacts, normalized_artifact),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def stage_generation_candidate(spec_path: Path, output_root: Path) -> dict[str, object]:
    """Stage an immutable candidate without advancing CURRENT.json."""

    catalog, manifest, catalog_bytes = build_generation(spec_path)
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / "schwab-generation.json"
    if catalog_path.exists() and catalog_path.read_bytes() != catalog_bytes:
        raise SchwabGenerationError("staged catalog conflicts with existing bytes")
    catalog_path.write_bytes(catalog_bytes)
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "coverage_status": manifest.coverage.status.value,
        "current_account_count": len(catalog.get("current_accounts", [])),  # type: ignore[arg-type]
        "generation_id": manifest.generation_id,
        "nav_count": len(catalog["nav_observations"]),  # type: ignore[arg-type]
        "position_count": len(catalog["positions"]),  # type: ignore[arg-type]
        "position_quantity_unavailable_count": catalog[
            "position_quantity_unavailable_count"
        ],
        "published": False,
        "transaction_count": len(catalog["transactions"]),  # type: ignore[arg-type]
    }


def publish_generation_candidate(
    spec_path: Path, output_root: Path, generation_id: str
) -> dict[str, object]:
    """Rebuild the exact accepted candidate and atomically publish it."""

    catalog, manifest, catalog_bytes = build_generation(spec_path)
    if manifest.generation_id != generation_id:
        raise SchwabGenerationError(
            "rebuilt generation differs from accepted candidate"
        )
    manifest_path = stage_generation(output_root, manifest)
    catalog_path = manifest_path.parent / "schwab-generation.json"
    if catalog_path.exists() and catalog_path.read_bytes() != catalog_bytes:
        raise SchwabGenerationError("staged catalog conflicts with existing bytes")
    if not catalog_path.exists():
        catalog_path.write_bytes(catalog_bytes)
    pointer = publish_generation(output_root, generation_id)
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "coverage_status": manifest.coverage.status.value,
        "generation_id": pointer.generation_id,
        "published": True,
    }
