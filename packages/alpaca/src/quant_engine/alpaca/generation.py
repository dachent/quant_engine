"""Hash-bound Alpaca legacy-export generation and single-authority cutover."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import Any, Final

from quant_engine.alpaca.native_activities import (
    NativeActivitiesError,
    NativePackage,
    load_native_package,
)
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
from quant_engine.portfolio_ledger import (
    MappingStatus,
    NavObservation,
    ObservedAmount,
    Transaction,
    decimal_text,
)


PLAN_SCHEMA_VERSION: Final = 1
CATALOG_SCHEMA_VERSION: Final = 1
ADAPTER_VERSION: Final = "0.1.0"
DEFAULT_SOURCE_FILE_COUNT: Final = 23
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_ID = re.compile(r"^account-[0-9a-f]{16}$")
_CAPTURE = re.compile(r"^alpaca_export_(\d{8})_\d{6}$")
_ORDER_STATUSES = frozenset({"canceled", "filled", "rejected"})
_ORDER_SIDES = frozenset({"buy", "sell"})


class AlpacaGenerationError(ValueError):
    """An Alpaca source, reconciliation, or cutover contract failed closed."""


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AlpacaGenerationError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AlpacaGenerationError(f"{name} must be an array")
    return [_object(item, f"{name}[]") for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlpacaGenerationError(f"{name} must be nonempty text")
    return value.strip()


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AlpacaGenerationError(f"{name} must be a nonnegative integer")
    return value


def _absolute_directory(value: object, name: str, *, existing: bool) -> Path:
    raw = str(value) if isinstance(value, Path) else _text(value, name)
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink():
        raise AlpacaGenerationError(f"{name} must be a real absolute directory")
    if existing and not path.is_dir():
        raise AlpacaGenerationError(f"{name} must exist")
    return path.resolve(strict=existing)


def _absolute_file(value: object, name: str) -> Path:
    raw = str(value) if isinstance(value, Path) else _text(value, name)
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise AlpacaGenerationError(f"{name} must be a real absolute file")
    return path.resolve(strict=True)


def _relative(value: object, name: str) -> str:
    raw = _text(value, name)
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise AlpacaGenerationError(f"{name} must be a portable relative path")
    return raw


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _entry_path(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if not _is_within(path.resolve(strict=False), root.resolve(strict=False)):
        raise AlpacaGenerationError("relative path escapes its root")
    return path


def _write_create_only(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise AlpacaGenerationError(f"existing evidence differs: {path.name}")
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _decimal(value: object, name: str) -> Decimal:
    raw = _text(value, name)
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise AlpacaGenerationError(f"{name} must be decimal") from exc
    if not parsed.is_finite():
        raise AlpacaGenerationError(f"{name} must be finite")
    return parsed


def _optional_decimal(value: object, name: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _decimal(value, name)


def _decimal_integer(value: object, name: str) -> int:
    parsed = _decimal(value, name)
    integral = parsed.to_integral_value()
    if parsed != integral or integral < 0:
        raise AlpacaGenerationError(f"{name} must be a nonnegative integer")
    return int(integral)


def _iso_datetime(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlpacaGenerationError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _capture_date(relative: str) -> date:
    parent = PurePosixPath(relative).parent.name
    match = _CAPTURE.fullmatch(parent)
    if match is None:
        raise AlpacaGenerationError("export directory name is not recognized")
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def _role(relative: str) -> str:
    path = PurePosixPath(relative)
    if path.parts[0] == "code":
        if path.suffix == ".py" or path.name == "requirements.txt":
            return "LEGACY_PROTOTYPE"
    name = path.name
    if name in {"account.csv", "account_info.csv"}:
        return "ACCOUNT_SNAPSHOT_CSV"
    if name in {"account.json", "account_info.json"}:
        return "ACCOUNT_SNAPSHOT_JSON"
    if name == "orders.csv":
        return "ORDER_HISTORY_CSV"
    if name.startswith("portfolio_history_") and name.endswith(".csv"):
        return "PORTFOLIO_HISTORY_CSV"
    if name in {"watchlists.csv", "watchlists_metadata.csv"}:
        return "WATCHLIST_CSV"
    if name == "historical_data.csv":
        return "MARKET_BARS_CSV"
    raise AlpacaGenerationError(f"legacy artifact is unclassified: {relative}")


def _schema(role: str) -> str:
    return {
        "ACCOUNT_SNAPSHOT_CSV": "alpaca-account-snapshot-csv",
        "ACCOUNT_SNAPSHOT_JSON": "alpaca-account-snapshot-json",
        "LEGACY_PROTOTYPE": "alpaca-legacy-prototype",
        "MARKET_BARS_CSV": "alpaca-market-bars-csv",
        "ORDER_HISTORY_CSV": "alpaca-order-history-csv",
        "PORTFOLIO_HISTORY_CSV": "alpaca-portfolio-history-csv",
        "WATCHLIST_CSV": "alpaca-watchlist-csv",
    }[role]


def _csv_metadata(path: Path) -> tuple[int, list[str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error, StopIteration) as exc:
        raise AlpacaGenerationError(f"CSV is unreadable: {path.name}") from exc
    if any(len(row) != len(header) for row in rows):
        raise AlpacaGenerationError(f"CSV row width differs: {path.name}")
    return len(rows), header


def _json_status(path: Path) -> str:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "INVALID_CAPTURE"
    return "VALID"


def _metadata(path: Path, root: Path) -> dict[str, object]:
    relative = path.relative_to(root).as_posix()
    role = _role(relative)
    digest, size = hash_file(path)
    result: dict[str, object] = {
        "bytes": size,
        "relative_path": relative,
        "role": role,
        "schema_name": _schema(role),
        "schema_version": "legacy-2026-07-17",
        "sha256": digest,
    }
    if path.suffix.lower() == ".csv":
        row_count, header = _csv_metadata(path)
        result.update(
            {
                "header_sha256": hashlib.sha256(
                    json.dumps(
                        header, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest(),
                "row_count": row_count,
            }
        )
    elif path.suffix.lower() == ".json":
        result["json_status"] = _json_status(path)
    return result


def _source_inventory(root: Path, expected_count: int) -> list[dict[str, object]]:
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    if any(path.is_symlink() for path in root.rglob("*")):
        raise AlpacaGenerationError("legacy source contains a linked path")
    if len(files) != expected_count:
        raise AlpacaGenerationError("source file count differs from accepted baseline")
    entries = [_metadata(path, root) for path in files]
    roles = Counter(str(item["role"]) for item in entries)
    expected_roles = {
        "ACCOUNT_SNAPSHOT_CSV": 3,
        "ACCOUNT_SNAPSHOT_JSON": 3,
        "LEGACY_PROTOTYPE": 8,
        "MARKET_BARS_CSV": 1,
        "ORDER_HISTORY_CSV": 1,
        "PORTFOLIO_HISTORY_CSV": 5,
        "WATCHLIST_CSV": 2,
    }
    if roles != expected_roles:
        raise AlpacaGenerationError("source role inventory differs")
    return entries


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise AlpacaGenerationError(f"CSV is unreadable: {path.name}") from exc


def _planned_path(root: Path, item: Mapping[str, Any]) -> Path:
    return _entry_path(root, _relative(item.get("relative_path"), "relative_path"))


def _entries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _objects(plan.get("entries"), "entries")


def _verify_tree(root: Path, plan: Mapping[str, Any]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise AlpacaGenerationError("input root is missing or linked")
    expected = {
        _relative(item.get("relative_path"), "relative_path"): item
        for item in _entries(plan)
    }
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
    }
    if set(actual) != set(expected):
        raise AlpacaGenerationError("input inventory differs from plan")
    for relative, item in expected.items():
        path = actual[relative]
        if path.is_symlink():
            raise AlpacaGenerationError("input contains a linked file")
        current = _metadata(path, root)
        if current != item:
            raise AlpacaGenerationError(f"planned artifact differs: {relative}")


def _rows_for_role(
    root: Path, plan: Mapping[str, Any], role: str
) -> list[tuple[str, list[dict[str, str]]]]:
    result: list[tuple[str, list[dict[str, str]]]] = []
    for item in _entries(plan):
        if item.get("role") == role:
            relative = _relative(item.get("relative_path"), "relative_path")
            result.append((relative, _read_csv(_planned_path(root, item))))
    return result


def _orders(root: Path, plan: Mapping[str, Any]) -> list[dict[str, str]]:
    values = _rows_for_role(root, plan, "ORDER_HISTORY_CSV")
    if len(values) != 1:
        raise AlpacaGenerationError("exactly one order export is required")
    rows = values[0][1]
    required = {
        "client_order_id",
        "created_at",
        "filled_at",
        "filled_avg_price",
        "filled_qty",
        "id",
        "order_type",
        "qty",
        "side",
        "status",
        "symbol",
        "time_in_force",
    }
    if not rows or not required <= set(rows[0]):
        raise AlpacaGenerationError("order export header is incomplete")
    if len({row["id"] for row in rows}) != len(rows) or len(
        {row["client_order_id"] for row in rows}
    ) != len(rows):
        raise AlpacaGenerationError("order identifiers are not unique")
    if {row["status"] for row in rows} - _ORDER_STATUSES:
        raise AlpacaGenerationError("order status is unsupported")
    if {row["side"] for row in rows} - _ORDER_SIDES:
        raise AlpacaGenerationError("order side is unsupported")
    return rows


def _order_fingerprint(row: Mapping[str, str]) -> tuple[str, str, str, str, str]:
    filled = _iso_datetime(row.get("filled_at"), "filled_at")
    return (
        filled.date().isoformat(),
        _text(row.get("side"), "side").lower(),
        _text(row.get("symbol"), "symbol").upper(),
        decimal_text(_decimal(row.get("filled_qty"), "filled_qty")),
        decimal_text(_decimal(row.get("filled_avg_price"), "filled_avg_price")),
    )


def _tracker_fingerprint(row: Mapping[str, str]) -> tuple[str, str, str, str, str]:
    kind = _text(row.get("type"), "tracker.type")
    if kind not in {"buy_fill", "sell_fill"}:
        raise AlpacaGenerationError("tracker row is not a fill")
    return (
        date.fromisoformat(_text(row.get("date"), "tracker.date")).isoformat(),
        kind.removesuffix("_fill"),
        _text(row.get("symbol"), "tracker.symbol").upper(),
        decimal_text(_decimal(row.get("qty"), "tracker.qty")),
        decimal_text(_decimal(row.get("price"), "tracker.price")),
    )


def _portfolio_history(
    root: Path, plan: Mapping[str, Any]
) -> tuple[list[dict[str, object]], int]:
    observed: dict[int, Decimal] = {}
    input_count = 0
    for relative, rows in _rows_for_role(root, plan, "PORTFOLIO_HISTORY_CSV"):
        for row in rows:
            timestamp = _integer(
                int(_text(row.get("timestamp"), "timestamp")), "timestamp"
            )
            equity = _decimal(row.get("equity"), "equity")
            prior = observed.get(timestamp)
            if prior is not None and prior != equity:
                raise AlpacaGenerationError("overlapping NAV observations conflict")
            observed[timestamp] = equity
            input_count += 1
        if not rows:
            raise AlpacaGenerationError(f"portfolio history is empty: {relative}")
    result = [
        {
            "as_of_date": datetime.fromtimestamp(timestamp, tz=timezone.utc)
            .date()
            .isoformat(),
            "equity": decimal_text(equity),
            "timestamp": timestamp,
        }
        for timestamp, equity in sorted(observed.items())
    ]
    return result, input_count


def _market_bars(root: Path, plan: Mapping[str, Any]) -> list[dict[str, object]]:
    values = _rows_for_role(root, plan, "MARKET_BARS_CSV")
    if len(values) != 1:
        raise AlpacaGenerationError("exactly one market-bars export is required")
    result: list[dict[str, object]] = []
    for row in values[0][1]:
        timestamp = _iso_datetime(row.get("timestamp"), "bar.timestamp")
        result.append(
            {
                "close": decimal_text(_decimal(row.get("close"), "bar.close")),
                "high": decimal_text(_decimal(row.get("high"), "bar.high")),
                "low": decimal_text(_decimal(row.get("low"), "bar.low")),
                "open": decimal_text(_decimal(row.get("open"), "bar.open")),
                "symbol": _text(row.get("symbol"), "bar.symbol").upper(),
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "trade_count": _decimal_integer(
                    row.get("trade_count"), "bar.trade_count"
                ),
                "volume": decimal_text(_decimal(row.get("volume"), "bar.volume")),
                "vwap": decimal_text(_decimal(row.get("vwap"), "bar.vwap")),
            }
        )
    result.sort(key=lambda item: (str(item["timestamp"]), str(item["symbol"])))
    return result


def _account_snapshots(
    root: Path, plan: Mapping[str, Any]
) -> tuple[list[dict[str, object]], int, int]:
    snapshots: list[dict[str, object]] = []
    identity_hashes: set[str] = set()
    invalid_json = 0
    for item in _entries(plan):
        role = item.get("role")
        relative = _relative(item.get("relative_path"), "relative_path")
        path = _planned_path(root, item)
        if role == "ACCOUNT_SNAPSHOT_JSON":
            if item.get("json_status") == "INVALID_CAPTURE":
                invalid_json += 1
                continue
            value = _object(json.loads(path.read_text(encoding="utf-8")), "account")
            if value.get("id"):
                identity_hashes.add(
                    hashlib.sha256(str(value["id"]).encode()).hexdigest()
                )
            continue
        if role != "ACCOUNT_SNAPSHOT_CSV":
            continue
        rows = _read_csv(path)
        if not rows or "id" not in rows[0]:
            continue
        if len(rows) != 1:
            raise AlpacaGenerationError("modern account snapshot must have one row")
        row = rows[0]
        identity_hashes.add(hashlib.sha256(row["id"].encode()).hexdigest())
        currency = _text(row.get("currency"), "account.currency")
        if currency != "USD":
            raise AlpacaGenerationError("account currency is unsupported")
        buying_power = _optional_decimal(row.get("buying_power"), "buying_power")
        cash = _optional_decimal(row.get("cash"), "cash")
        snapshots.append(
            {
                "as_of_date": row.get("balance_asof")
                or _capture_date(relative).isoformat(),
                "buying_power": (
                    decimal_text(buying_power) if buying_power is not None else None
                ),
                "cash": decimal_text(cash) if cash is not None else None,
                "capture_date": _capture_date(relative).isoformat(),
                "currency": currency,
                "equity": decimal_text(_decimal(row.get("equity"), "account.equity")),
                "portfolio_value": decimal_text(
                    _decimal(row.get("portfolio_value"), "account.portfolio_value")
                ),
                "status": _text(row.get("status"), "account.status"),
            }
        )
    if len(identity_hashes) != 1:
        raise AlpacaGenerationError("account identity differs across valid snapshots")
    snapshots.sort(
        key=lambda item: (str(item["capture_date"]), str(item["as_of_date"]))
    )
    return snapshots, len(identity_hashes), invalid_json


def _tracker_rows(plan: Mapping[str, Any]) -> list[dict[str, str]]:
    tracker = _object(plan.get("tracker_events"), "tracker_events")
    path = _absolute_file(tracker.get("path"), "tracker_events.path")
    digest, size = hash_file(path)
    if digest != tracker.get("sha256") or size != tracker.get("bytes"):
        raise AlpacaGenerationError("tracker reconciliation source differs")
    rows = _read_csv(path)
    if len(rows) != tracker.get("row_count"):
        raise AlpacaGenerationError("tracker reconciliation row count differs")
    return rows


def create_plan(
    source_root: Path,
    data_root: Path,
    tracker_events_path: Path,
    *,
    internal_account_id: str,
    as_of_date: date,
    expected_source_file_count: int = DEFAULT_SOURCE_FILE_COUNT,
    native_activities: Path | None = None,
) -> dict[str, object]:
    """Create one content-addressed, local-only Alpaca cutover plan.

    ``native_activities`` optionally names an ingest package (see
    ``quant_engine.alpaca.native_activities``).  Without it the plan, and so the
    generation, is byte-identical to the legacy-only form: the block is added to
    the plan only when one is supplied.
    """

    source = _absolute_directory(source_root, "source_root", existing=True)
    destination = _absolute_directory(data_root, "data_root", existing=True)
    tracker = _absolute_file(tracker_events_path, "tracker_events_path")
    if _is_within(destination, source) or _is_within(source, destination):
        raise AlpacaGenerationError("source and data roots must be disjoint")
    if _ACCOUNT_ID.fullmatch(internal_account_id) is None:
        raise AlpacaGenerationError("internal account ID is invalid")
    entries = _source_inventory(source, expected_source_file_count)
    order_rows = _orders(source, {"entries": entries})
    nav_rows, nav_input_count = _portfolio_history(source, {"entries": entries})
    account_snapshots, identity_count, invalid_json = _account_snapshots(
        source, {"entries": entries}
    )
    market_bars = _market_bars(source, {"entries": entries})
    tracker_hash, tracker_size = hash_file(tracker)
    tracker_rows = _read_csv(tracker)
    tracker_fills = [
        row for row in tracker_rows if row.get("type") in {"buy_fill", "sell_fill"}
    ]
    tracker_other = [row for row in tracker_rows if row not in tracker_fills]
    if {row.get("type") for row in tracker_other} != {
        "deposit",
        "withdrawal_inferred",
    }:
        raise AlpacaGenerationError("tracker non-order disposition differs")
    filled = [row for row in order_rows if row["status"] == "filled"]
    if Counter(_order_fingerprint(row) for row in filled) != Counter(
        _tracker_fingerprint(row) for row in tracker_fills
    ):
        raise AlpacaGenerationError("filled orders do not reconcile to tracker fills")
    payload: dict[str, object] = {
        "as_of_date": as_of_date.isoformat(),
        "baseline": {
            "account_identity_count": identity_count,
            "account_snapshot_count": len(account_snapshots),
            "filled_order_count": len(filled),
            "invalid_account_json_count": invalid_json,
            "market_bar_count": len(market_bars),
            "nav_input_count": nav_input_count,
            "nav_observation_count": len(nav_rows),
            "order_count": len(order_rows),
            "source_file_count": len(entries),
            "tracker_fill_count": len(tracker_fills),
            "tracker_non_order_count": len(tracker_other),
        },
        "data_root": str(destination),
        "entries": entries,
        "internal_account_id": internal_account_id,
        "provider_id": "alpaca",
        "schema_version": PLAN_SCHEMA_VERSION,
        "source_root": str(source),
        "tracker_events": {
            "bytes": tracker_size,
            "path": str(tracker),
            "row_count": len(tracker_rows),
            "schema_name": "tracker-alpaca-events",
            "schema_version": "legacy-2026-07-17",
            "sha256": tracker_hash,
        },
    }
    if native_activities is not None:
        package = _native_package(
            {
                "directory": str(
                    _absolute_directory(
                        native_activities, "native_activities", existing=True
                    )
                )
            }
        )
        payload["native_activities"] = {
            "binding_sha256": package.binding_sha256,
            "directory": str(package.directory),
            "internal_account_id": package.internal_account_id,
        }
    plan_id = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    run_id = f"legacy-{as_of_date.strftime('%Y%m%d')}-{plan_id[:12]}"
    plan = {**payload, "plan_id": plan_id, "run_id": run_id}
    plan_path = destination / "cutovers" / "plans" / plan_id / "plan.json"
    _write_create_only(plan_path, canonical_json_bytes(plan))
    return {
        "filled_order_count": len(filled),
        "nav_observation_count": len(nav_rows),
        "plan_id": plan_id,
        "plan_path": str(plan_path),
        "run_id": run_id,
        "source_file_count": len(entries),
        "state": "PLANNED",
    }


def _load_plan(plan_path: Path) -> dict[str, Any]:
    try:
        plan = _object(json.loads(plan_path.read_text(encoding="utf-8")), "plan")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlpacaGenerationError("plan is not valid UTF-8 JSON") from exc
    identity = dict(plan)
    plan_id = identity.pop("plan_id", None)
    run_id = identity.pop("run_id", None)
    if not isinstance(plan_id, str) or _SHA256.fullmatch(plan_id) is None:
        raise AlpacaGenerationError("plan ID is invalid")
    if hashlib.sha256(canonical_json_bytes(identity)).hexdigest() != plan_id:
        raise AlpacaGenerationError("plan identity differs")
    expected_run = (
        f"legacy-{str(plan.get('as_of_date', '')).replace('-', '')}-{plan_id[:12]}"
    )
    if run_id != expected_run or plan_path.parent.name != plan_id:
        raise AlpacaGenerationError("plan run/directory identity differs")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise AlpacaGenerationError("plan schema version is unsupported")
    return plan


def _authority_paths(plan: Mapping[str, Any]) -> tuple[Path, Path]:
    data_root = _absolute_directory(plan.get("data_root"), "data_root", existing=True)
    run_root = data_root / "source-runs" / _text(plan.get("run_id"), "run_id")
    return run_root / ".staging", run_root / "artifacts"


def _source_artifacts(
    plan: Mapping[str, Any], generation_as_of: date
) -> tuple[SourceArtifact, ...]:
    artifacts = [
        SourceArtifact(
            artifact_id=f"legacy/{_relative(item.get('relative_path'), 'relative_path')}",
            sha256=_text(item.get("sha256"), "sha256"),
            size_bytes=_integer(item.get("bytes"), "bytes"),
            as_of_date=generation_as_of,
            schema_name=_text(item.get("schema_name"), "schema_name"),
            schema_version=_text(item.get("schema_version"), "schema_version"),
        )
        for item in _entries(plan)
    ]
    tracker = _object(plan.get("tracker_events"), "tracker_events")
    artifacts.append(
        SourceArtifact(
            artifact_id="reconciliation/portfolio-tracker/alpaca_events.csv",
            sha256=_text(tracker.get("sha256"), "tracker.sha256"),
            size_bytes=_integer(tracker.get("bytes"), "tracker.bytes"),
            as_of_date=generation_as_of,
            schema_name=_text(tracker.get("schema_name"), "tracker.schema_name"),
            schema_version=_text(
                tracker.get("schema_version"), "tracker.schema_version"
            ),
        )
    )
    return tuple(artifacts)


def _native_package(block: Mapping[str, Any]) -> NativePackage:
    """Load the plan's native activities package, re-validated from its bytes."""

    pinned = block.get("binding_sha256")
    try:
        package = load_native_package(
            _text(block.get("directory"), "native_activities.directory"),
            binding_sha256=pinned if isinstance(pinned, str) else None,
        )
    except NativeActivitiesError as exc:
        raise AlpacaGenerationError(str(exc)) from exc
    declared = block.get("internal_account_id")
    if declared is not None and declared != package.internal_account_id:
        raise AlpacaGenerationError("native activities account differs from the plan")
    return package


def _native_transaction(account_id: str, row: Mapping[str, Any]) -> dict[str, object]:
    """One ledger transaction for one native activity (cash leg as observed)."""

    digest = str(row["activity_id_sha256"])
    cash = row["cash_amount"]
    amount = (
        ObservedAmount.available(Decimal(str(cash)), "USD")
        if cash is not None
        else ObservedAmount.unavailable("USD")
    )
    symbol = row["symbol"]
    transaction = Transaction(
        record_id=_record_id(
            "transaction", {"account_id": account_id, "source_id_hash": digest}
        ),
        account_id=account_id,
        event_date=date.fromisoformat(str(row["event_date"])),
        kind=f"ACTIVITY_{row['activity_type']}",
        amount=amount,
        mapping_status=MappingStatus.UNMAPPED,
        security_id=_security_id(str(symbol).upper()) if symbol is not None else None,
    ).to_dict()
    transaction.update(
        {
            "activity_type": row["activity_type"],
            "cash_amount_basis": row["cash_amount_basis"],
            "category": row["category"],
            "price": row["price"],
            "quantity": row["quantity"],
            "side": row["side"],
            "source_activity_id_sha256": digest,
        }
    )
    return transaction


def _native_artifacts(
    native: NativePackage, generation_as_of: date
) -> tuple[SourceArtifact, ...]:
    schema = {
        "native-activities.jsonl": "alpaca-native-activities-jsonl",
        "native-account.json": "alpaca-native-account-json",
        "native-positions.json": "alpaca-native-positions-json",
        "native-activities-spec.json": "alpaca-native-activities-spec",
        "ingest-binding.json": "alpaca-native-ingest-binding",
    }
    return tuple(
        SourceArtifact(
            artifact_id=f"native/{item.name}",
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            as_of_date=generation_as_of,
            schema_name=schema[item.name],
            schema_version="1",
        )
        for item in native.files
    )


def _record_id(prefix: str, value: Mapping[str, object]) -> str:
    return f"{prefix}-{hashlib.sha256(canonical_json_bytes(value)).hexdigest()[:24]}"


def _security_id(symbol: str) -> str:
    return f"security-alpaca-{hashlib.sha256(symbol.encode()).hexdigest()[:24]}"


def build_generation(
    plan_path: Path, *, input_root: Path | None = None
) -> tuple[dict[str, object], GenerationManifest, bytes]:
    """Build the exact observed export boundary and ledger projection."""

    plan = _load_plan(plan_path)
    root = (
        _absolute_directory(input_root, "input_root", existing=True)
        if input_root is not None
        else _absolute_directory(plan.get("source_root"), "source_root", existing=True)
    )
    _verify_tree(root, plan)
    generation_as_of = date.fromisoformat(_text(plan.get("as_of_date"), "as_of_date"))
    account_id = _text(plan.get("internal_account_id"), "internal_account_id")
    baseline = _object(plan.get("baseline"), "baseline")
    order_rows = _orders(root, plan)
    nav_rows, nav_input_count = _portfolio_history(root, plan)
    account_snapshots, identity_count, invalid_json = _account_snapshots(root, plan)
    market_bars = _market_bars(root, plan)
    tracker_rows = _tracker_rows(plan)
    tracker_fills = [
        row for row in tracker_rows if row.get("type") in {"buy_fill", "sell_fill"}
    ]
    tracker_other = [row for row in tracker_rows if row not in tracker_fills]
    filled = [row for row in order_rows if row["status"] == "filled"]
    if Counter(_order_fingerprint(row) for row in filled) != Counter(
        _tracker_fingerprint(row) for row in tracker_fills
    ):
        raise AlpacaGenerationError("filled orders do not reconcile to tracker fills")

    actual = {
        "account_identity_count": identity_count,
        "account_snapshot_count": len(account_snapshots),
        "filled_order_count": len(filled),
        "invalid_account_json_count": invalid_json,
        "market_bar_count": len(market_bars),
        "nav_input_count": nav_input_count,
        "nav_observation_count": len(nav_rows),
        "order_count": len(order_rows),
        "source_file_count": len(_entries(plan)),
        "tracker_fill_count": len(tracker_fills),
        "tracker_non_order_count": len(tracker_other),
    }
    if actual != baseline:
        raise AlpacaGenerationError("observed baseline differs from the accepted plan")

    normalized_orders: list[dict[str, object]] = []
    transactions: list[dict[str, object]] = []
    symbols: set[str] = set()
    event_dates: list[date] = []
    for row in order_rows:
        created = _iso_datetime(row["created_at"], "created_at")
        symbol = _text(row["symbol"], "symbol").upper()
        symbols.add(symbol)
        identity = {
            "created_at": created.isoformat(),
            "side": row["side"],
            "symbol": symbol,
            "source_id_hash": hashlib.sha256(row["id"].encode()).hexdigest(),
        }
        normalized: dict[str, object] = {
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "filled_at": None,
            "filled_avg_price": None,
            "filled_qty": None,
            "order_id": _record_id("order", identity),
            "order_type": _text(row["order_type"], "order_type"),
            "qty": decimal_text(_decimal(row["qty"], "qty")),
            "side": row["side"],
            "status": row["status"],
            "symbol": symbol,
            "time_in_force": _text(row["time_in_force"], "time_in_force"),
        }
        if row["status"] == "filled":
            filled_at = _iso_datetime(row["filled_at"], "filled_at")
            filled_qty = _decimal(row["filled_qty"], "filled_qty")
            filled_price = _decimal(row["filled_avg_price"], "filled_avg_price")
            normalized.update(
                {
                    "filled_at": filled_at.isoformat().replace("+00:00", "Z"),
                    "filled_avg_price": decimal_text(filled_price),
                    "filled_qty": decimal_text(filled_qty),
                }
            )
            event_dates.append(filled_at.date())
            signed_amount = filled_qty * filled_price
            if row["side"] == "buy":
                signed_amount = -signed_amount
            transaction = Transaction(
                record_id=_record_id("transaction", identity),
                account_id=account_id,
                event_date=filled_at.date(),
                kind=f"ORDER_FILL_{row['side'].upper()}",
                amount=ObservedAmount.available(signed_amount, "USD"),
                mapping_status=MappingStatus.UNMAPPED,
                security_id=_security_id(symbol),
            ).to_dict()
            transaction.update(
                {
                    "price": decimal_text(filled_price),
                    "quantity": decimal_text(filled_qty),
                    "source_order_id": normalized["order_id"],
                }
            )
            transactions.append(transaction)
        normalized_orders.append(normalized)
    normalized_orders.sort(
        key=lambda item: (str(item["created_at"]), str(item["order_id"]))
    )
    native: NativePackage | None = None
    native_block = plan.get("native_activities")
    if native_block is not None:
        native_object = _object(native_block, "native_activities")
        if not isinstance(native_object.get("binding_sha256"), str):
            raise AlpacaGenerationError("native activities binding is not pinned")
        native = _native_package(native_object)
        used_ids = {str(item["record_id"]) for item in transactions}
        for row in native.rows:
            native_transaction = _native_transaction(native.internal_account_id, row)
            record_id = str(native_transaction["record_id"])
            if record_id in used_ids:
                raise AlpacaGenerationError("native activity record id collides")
            used_ids.add(record_id)
            transactions.append(native_transaction)
            event_dates.append(date.fromisoformat(str(row["event_date"])))
            if row["symbol"] is not None:
                symbols.add(str(row["symbol"]).upper())
    transactions.sort(
        key=lambda item: (str(item["event_date"]), str(item["record_id"]))
    )

    nav_observations: list[dict[str, object]] = []
    for item in nav_rows:
        as_of = date.fromisoformat(str(item["as_of_date"]))
        event_dates.append(as_of)
        nav_observations.append(
            NavObservation(
                record_id=_record_id(
                    "nav", {"account_id": account_id, "as_of_date": as_of.isoformat()}
                ),
                account_id=account_id,
                as_of_date=as_of,
                nav=ObservedAmount.available(Decimal(str(item["equity"])), "USD"),
            ).to_dict()
        )
    for bar in market_bars:
        event_dates.append(
            _iso_datetime(str(bar["timestamp"]), "market_bar.timestamp").date()
        )
    if not event_dates:
        raise AlpacaGenerationError("generation has no dated observations")

    status_counts = Counter(row["status"] for row in order_rows)
    side_counts = Counter(row["side"] for row in filled)
    artifacts = _source_artifacts(plan, generation_as_of)
    out_of_boundary = [
        "account activities were not exported; no provider-observed cash flows are published",
        "positions were not exported; no position snapshot is published",
        "order history spans only the observed 2025-09-30 through 2025-11-24 interval",
        "portfolio history is a finite 62-observation snapshot ending 2026-07-17",
        "one 11-byte account JSON capture is truncated and retained only as failed evidence",
        "the tracker deposit remains tracker evidence and its withdrawal remains explicitly inferred",
    ]
    if native is not None:
        artifacts = (*artifacts, *_native_artifacts(native, generation_as_of))
        out_of_boundary[0] = (
            "account activities are published only for the native capture window "
            f"{native.after} through {native.until} of the native account; the "
            "legacy account has no provider-observed cash flows"
        )
        if native.positions is not None:
            out_of_boundary[1] = (
                "positions were not exported for the legacy account; the native "
                "account carries one current positions snapshot in native_activities"
            )
    catalog: dict[str, object] = {
        "account_snapshots": account_snapshots,
        "accounts": [
            {
                "account_id": account_id,
                "base_currency": "USD",
                "provider": "ALPACA",
            }
        ],
        "as_of_date": generation_as_of.isoformat(),
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": {
            "known_gaps": [],
            "observed_end": max(event_dates).isoformat(),
            "observed_start": min(event_dates).isoformat(),
            "out_of_boundary": out_of_boundary,
            "status": "COMPLETE_WITHIN_DECLARED_BOUNDARY",
        },
        "market_bars": market_bars,
        "nav_observations": nav_observations,
        "orders": normalized_orders,
        "positions": [],
        "provenance": {
            "invalid_account_json_count": invalid_json,
            "legacy_prototype_count": sum(
                item.get("role") == "LEGACY_PROTOTYPE" for item in _entries(plan)
            ),
            "source_file_count": len(_entries(plan)),
        },
        "reconciliation": {
            "filled_order_count": len(filled),
            "ledger_transaction_count": len(transactions),
            "nav_input_count": nav_input_count,
            "nav_observation_count": len(nav_observations),
            "order_status_counts": dict(sorted(status_counts.items())),
            "status": "PASS_EXACT",
            "tracker_fill_count": len(tracker_fills),
            "tracker_fill_difference_count": 0,
            "tracker_non_order_disposition": {
                "deposit": "TRACKER_ONLY_NOT_PROVIDER_ACTIVITY",
                "withdrawal_inferred": "INFERRED_TRACKER_EVIDENCE",
            },
            "tracker_non_order_count": len(tracker_other),
        },
        "securities": [
            {
                "currency": "USD",
                "security_id": _security_id(symbol),
                "symbol": symbol,
            }
            for symbol in sorted(symbols)
        ],
        "source_artifacts": [item.to_dict() for item in artifacts],
        "transactions": transactions,
        "watchlists": {
            "metadata_only": True,
            "source_row_count": sum(
                len(rows) for _, rows in _rows_for_role(root, plan, "WATCHLIST_CSV")
            ),
        },
        "filled_side_counts": dict(sorted(side_counts.items())),
    }
    if native is not None:
        accounts = catalog["accounts"]
        assert isinstance(accounts, list)
        if native.internal_account_id != account_id:
            accounts.append(
                {
                    "account_id": native.internal_account_id,
                    "base_currency": "USD",
                    "provider": "ALPACA",
                }
            )
        reconciliation = catalog["reconciliation"]
        assert isinstance(reconciliation, dict)
        reconciliation["native_activity_transaction_count"] = len(native.rows)
        catalog["native_activities"] = {
            "account_snapshot": native.account,
            "binding_sha256": native.binding_sha256,
            "internal_account_id": native.internal_account_id,
            "positions": native.positions,
            "source_run": native.spec["source_run"],
            "summary": native.summary,
            "window": {"after": native.after, "until": native.until},
        }
    catalog_bytes = canonical_json_bytes(catalog)
    normalized_artifact = SourceArtifact(
        artifact_id="normalized/alpaca-generation.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        size_bytes=len(catalog_bytes),
        as_of_date=generation_as_of,
        schema_name="alpaca-read-generation",
        schema_version=str(CATALOG_SCHEMA_VERSION),
    )
    boundary = (
        "23 hash-bound legacy Alpaca code/data artifacts plus exact "
        "reconciliation to the accepted 122-row portfolio tracker slice"
    )
    fact_types = (
        "account_snapshots",
        "ledger_transactions",
        "market_bars",
        "nav_observations",
        "orders",
        "watchlist_metadata",
    )
    if native is not None:
        boundary += (
            f"; plus {len(native.rows)} hash-bound native account activities "
            f"for {native.after} through {native.until}"
        )
        fact_types = tuple(
            sorted(
                {
                    *fact_types,
                    "account_activities",
                    *(("positions_snapshot",) if native.positions is not None else ()),
                }
            )
        )
    manifest = GenerationManifest.build(
        as_of_date=generation_as_of,
        adapter=AdapterIdentity("quant-engine-alpaca", ADAPTER_VERSION),
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=boundary,
            fact_types=fact_types,
            observed_start=min(event_dates),
            observed_end=max(event_dates),
            known_gaps=(),
        ),
        artifacts=(*artifacts, normalized_artifact),
        verification=VerificationStatus.VERIFIED,
    )
    return catalog, manifest, catalog_bytes


def _copy_to_staging(plan: Mapping[str, Any]) -> Path:
    source = _absolute_directory(plan.get("source_root"), "source_root", existing=True)
    _verify_tree(source, plan)
    staging, authority = _authority_paths(plan)
    if authority.exists():
        _verify_tree(authority, plan)
        return authority
    staging.mkdir(parents=True, exist_ok=True)
    for item in _entries(plan):
        relative = _relative(item.get("relative_path"), "relative_path")
        source_path = _entry_path(source, relative)
        destination = _entry_path(staging, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            digest, size = hash_file(destination)
            if digest != item.get("sha256") or size != item.get("bytes"):
                raise AlpacaGenerationError("staging contains conflicting bytes")
        else:
            shutil.copy2(source_path, destination)
    _verify_tree(staging, plan)
    return staging


def stage_candidate(plan_path: Path) -> dict[str, object]:
    plan = _load_plan(plan_path)
    staging = _copy_to_staging(plan)
    catalog, manifest, catalog_bytes = build_generation(plan_path, input_root=staging)
    data_root = _absolute_directory(plan.get("data_root"), "data_root", existing=True)
    manifest_path = stage_generation(data_root, manifest)
    _write_create_only(manifest_path.parent / "alpaca-generation.json", catalog_bytes)
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "generation_id": manifest.generation_id,
        "nav_observation_count": len(catalog["nav_observations"]),  # type: ignore[arg-type]
        "published": False,
        "state": "STAGED_AND_VERIFIED",
        "transaction_count": len(catalog["transactions"]),  # type: ignore[arg-type]
    }


def publish_candidate(plan_path: Path, generation_id: str) -> dict[str, object]:
    plan = _load_plan(plan_path)
    staged = stage_candidate(plan_path)
    if staged["generation_id"] != generation_id:
        raise AlpacaGenerationError("rebuilt generation differs from candidate")
    staging, authority = _authority_paths(plan)
    if not authority.exists():
        authority.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(authority)
    _verify_tree(authority, plan)
    _, manifest, catalog_bytes = build_generation(plan_path, input_root=authority)
    if manifest.generation_id != generation_id:
        raise AlpacaGenerationError("authority generation differs")
    data_root = _absolute_directory(plan.get("data_root"), "data_root", existing=True)
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
    _verify_tree(authority, plan)
    _, manifest, catalog_bytes = build_generation(plan_path, input_root=authority)
    if manifest.generation_id != generation_id:
        raise AlpacaGenerationError("rebuilt authority differs")
    data_root = _absolute_directory(plan.get("data_root"), "data_root", existing=True)
    if read_current(data_root).generation_id != generation_id:
        raise AlpacaGenerationError("CURRENT.json points to another generation")
    catalog_path = data_root / "generations" / generation_id / "alpaca-generation.json"
    if catalog_path.read_bytes() != catalog_bytes:
        raise AlpacaGenerationError("published catalog differs")
    return {
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "generation_id": generation_id,
        "state": "VERIFIED_PUBLISHED",
    }


def _remove_empty_directory(path: Path) -> None:
    if os.name == "nt":
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    path.rmdir()


def cutover_source(plan_path: Path, generation_id: str) -> dict[str, object]:
    """Remove only the exact accepted legacy tree after publication proof."""

    plan = _load_plan(plan_path)
    verify_published(plan_path, generation_id)
    source = Path(_text(plan.get("source_root"), "source_root"))
    if not source.exists():
        result_path = plan_path.parent / "cutover-result.json"
        return _object(json.loads(result_path.read_text(encoding="utf-8")), "result")
    source = _absolute_directory(source, "source_root", existing=True)
    _verify_tree(source, plan)
    for item in _entries(plan):
        _planned_path(source, item).unlink()
    for directory in sorted(
        (path for path in source.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        _remove_empty_directory(directory)
    _remove_empty_directory(source)
    _, authority = _authority_paths(plan)
    result = {
        "generation_id": generation_id,
        "original_source_removed": True,
        "plan_id": plan["plan_id"],
        "recoverable_from": str(authority),
        "state": "SINGLE_AUTHORITY_CUTOVER_COMPLETE",
    }
    _write_create_only(
        plan_path.parent / "cutover-result.json", canonical_json_bytes(result)
    )
    return result
