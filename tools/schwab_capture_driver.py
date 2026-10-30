"""Governed Schwab capture driver: cutoff-close holdings, balances, transactions.

This extends ``tools/schwab_transaction_capture.py`` (transaction paging, WCH
transaction shape) with the two families that tool does not cover, and wraps all
three in one live-read governance gate, one create-only raw staging root, one
hash-bound manifest and an independent verifier.

What it refuses, by design:

* any live read without current, single-use, reviewed governance evidence whose
  scope names the exact read-only requests and the declared cutoff;
* holdings whose per-row as-of date differs from the declared cutoff close,
  unless the run carries an explicit exception label (the rows are then labelled
  with their offset);
* a capture taken before the cutoff close, or later than the allowed lag after
  it, unless the same explicit label is present;
* any held position (nonzero quantity) whose market value is null, missing or
  not a finite decimal. The older Holdings-V2 normalizer in
  ``quant_engine.schwab`` records such rows as unavailable; this driver does not;
* unknown response shapes, duplicate identities, accounts outside the crosswalk,
  and overwriting any staged file.

The Holdings-V2 field names beyond those in the repository's synthetic fixtures
(notably the per-row price date) and the historical-balance response shape are
UNVERIFIED against a live response. The driver fails closed on anything else.
Nothing here is exercised live by the repository tests.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from quant_engine.core import strict_json_loads
from quant_engine.web_contracts import SCHWAB_CONTRACT


# ``tools/schwab_transaction_capture.py`` is outside the strict mypy list (it
# carries pre-existing strict errors) and must stay byte-for-byte unchanged, so
# it is loaded by name here: a static import would pull its errors into this
# module's type check. Only its public API is used.
_TRANSACTION_CAPTURE: Any = importlib.import_module("schwab_transaction_capture")
SchwabCaptureError: type[ValueError] = _TRANSACTION_CAPTURE.SchwabCaptureError


class TargetLike(Protocol):
    """One retained account: ``schwab_transaction_capture.AccountTarget``."""

    @property
    def internal_account_id(self) -> str: ...

    @property
    def provider_account_id(self) -> str: ...

    @property
    def provider_account_nickname(self) -> str: ...


DRIVER_VERSION = "1"
SCHEMA_VERSION = 1
NORMALIZER_VERSION = "schwab-capture-normalizer-1"
CLOSE_TIME_ZONE = "America/New_York"
CLOSE_LOCAL_TIME = time(16, 0)
DEFAULT_MAX_CAPTURE_LAG_HOURS = 8
TERMS_VERSION = "schwab-live-read-terms-v1"
TERMS_TEXT = (
    "Schwab live reads require one current, reviewed, single-use approval; "
    "read-only requests named exactly in scope; the declared cutoff; a "
    "zero-retry transport; and a durable pre-network request reservation."
)
TERMS_SHA256 = hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()
UNVERIFIED_SHAPES = (
    "holdings.v2 row price date field (priceDate)",
    "holdings.v2 route path (owner supplied)",
    "balances.historical request query and response shape",
)

_MAX_EVIDENCE_BYTES = 64 * 1024
_MAX_REQUESTS = 10_000
_ACCOUNT_ID = re.compile(r"account-[0-9a-f]{16}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}\Z")
_LABEL = re.compile(r"[A-Z][A-Z0-9_]{5,63}\Z")
_SAFE_PATH = re.compile(r"/api/[A-Za-z0-9._/-]{1,200}\Z")
_SAFE_QUERY_VALUE = re.compile(r"[A-Za-z0-9._:/-]{0,64}\Z")
_SAFE_QUERY_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,40}\Z")
_FORBIDDEN_PATH_WORDS = (
    "order",
    "cancel",
    "transfer",
    "payment",
    "bill",
    "place",
    "replace",
    "withdraw",
    "wire",
)
_REVIEW_REFERENCE = re.compile(
    r"https://github\.com/dachent/quant_engine/issues/[1-9][0-9]*#issuecomment-[1-9][0-9]*\Z"
)
_OBSERVED_HEADER_NAMES = (
    "authorization",
    "correlatorid",
    "schwab-channelcode",
    "schwab-client-appid",
    "schwab-client-channel",
    "schwab-client-correlid",
    "schwab-clientapp-name",
    "schwab-env",
    "schwab-environment",
    "schwab-resource-version",
)
_SWEEP_GROUP_KINDS = {
    ("equity", 1): "position",
    ("etf", 2): "position",
    ("cash", 9): "cash",
}
_MISSING_TEXT = frozenset({"", "--", "N/A"})


class DriverError(ValueError):
    """Governance, shape, as-of or integrity rule violated; no value is echoed."""


class GovernanceError(DriverError):
    """Live-read governance evidence is absent, invalid, consumed or exhausted."""


# --------------------------------------------------------------------------- #
# Catalog of approved reads
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CatalogEntry:
    capability: str
    method: str
    path: str | None
    route_status: str
    safety_class: str = "read_only"

    def row(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "method": self.method,
            "path": self.path,
            "route_status": self.route_status,
            "safety_class": self.safety_class,
        }


def _contract_path(capability: str) -> str:
    for endpoint in SCHWAB_CONTRACT.endpoints:
        if endpoint.capability == capability:
            return str(endpoint.path)
    raise DriverError("reviewed Schwab contract no longer names the capability")


def catalog() -> tuple[CatalogEntry, ...]:
    """The only reads the driver can be approved for, in a fixed order."""

    return (
        CatalogEntry(
            "balances.historical",
            "GET",
            _contract_path("balances.historical"),
            "REVIEWED_CONTRACT_PATH_UNVERIFIED_QUERY",
        ),
        CatalogEntry("holdings.v2", "GET", None, "OWNER_SUPPLIED_UNVERIFIED"),
        CatalogEntry(
            "transactions.history",
            "POST",
            _contract_path("transactions.history"),
            "REVIEWED_CONTRACT",
        ),
    )


def catalog_sha256() -> str:
    return hashlib.sha256(_canonical([entry.row() for entry in catalog()])).hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# Cutoff and as-of semantics
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Cutoff:
    """The declared cutoff close: ``as_of_date`` at 16:00 New York."""

    as_of_date: date

    @property
    def close_utc(self) -> datetime:
        local = datetime.combine(
            self.as_of_date, CLOSE_LOCAL_TIME, tzinfo=ZoneInfo(CLOSE_TIME_ZONE)
        )
        return local.astimezone(timezone.utc)

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "close_time_zone": CLOSE_TIME_ZONE,
            "close_utc": self.close_utc.isoformat(),
        }


def parse_cutoff(value: object) -> Cutoff:
    if not isinstance(value, str):
        raise DriverError("cutoff date must be ISO text")
    try:
        return Cutoff(date.fromisoformat(value))
    except ValueError as exc:
        raise DriverError("cutoff date must be an ISO date") from exc


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise DriverError(f"{name} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DriverError(f"{name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise DriverError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _label(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        raise DriverError("exception label must be an upper-case token")
    return value


@dataclass(frozen=True)
class CaptureTiming:
    """Capture time against the declared close; recorded on every output."""

    captured_at: datetime
    cutoff: Cutoff
    lag_hours: Decimal
    capture_date_utc: date
    capture_date_new_york: date

    def to_dict(self) -> dict[str, object]:
        return {
            "capture_date_new_york": self.capture_date_new_york.isoformat(),
            "capture_date_utc": self.capture_date_utc.isoformat(),
            "capture_lag_hours": format(self.lag_hours, "f"),
            "captured_at_utc": self.captured_at.isoformat(),
        }


def assess_timing(
    captured_at: datetime,
    cutoff: Cutoff,
    *,
    exception_label: str | None,
    max_lag_hours: int = DEFAULT_MAX_CAPTURE_LAG_HOURS,
) -> CaptureTiming:
    if captured_at.tzinfo is None:
        raise DriverError("capture time must be timezone-aware")
    captured = captured_at.astimezone(timezone.utc)
    lag = Decimal((captured - cutoff.close_utc).total_seconds()) / Decimal(3600)
    timing = CaptureTiming(
        captured_at=captured,
        cutoff=cutoff,
        lag_hours=lag.quantize(Decimal("0.01")),
        capture_date_utc=captured.date(),
        capture_date_new_york=captured.astimezone(ZoneInfo(CLOSE_TIME_ZONE)).date(),
    )
    if exception_label is None:
        if captured < cutoff.close_utc:
            raise DriverError("capture precedes the declared cutoff close")
        if lag > Decimal(max_lag_hours):
            raise DriverError("capture is later than the allowed lag after the close")
    return timing


# --------------------------------------------------------------------------- #
# Crosswalk
# --------------------------------------------------------------------------- #


def parse_crosswalk(document: object) -> dict[str, str]:
    """External provider id to internal account id; exact and one-to-one."""

    rows: object = document
    if isinstance(document, dict):
        rows = document.get("crosswalk")
    if not isinstance(rows, list) or not rows:
        raise DriverError("crosswalk must be a nonempty list")
    result: dict[str, str] = {}
    for item in rows:
        if not isinstance(item, dict):
            raise DriverError("crosswalk entries must be objects")
        external = item.get("external_account_id")
        internal = item.get("internal_account_id")
        if (
            not isinstance(external, str)
            or not external.strip()
            or external == "[REDACTED]"
            or not isinstance(internal, str)
            or _ACCOUNT_ID.fullmatch(internal) is None
        ):
            raise DriverError("crosswalk entry is invalid")
        if external in result:
            raise DriverError("crosswalk external account is duplicated")
        result[external] = internal
    if len(set(result.values())) != len(result):
        raise DriverError("crosswalk is not one-to-one")
    return result


# --------------------------------------------------------------------------- #
# Normalizers
# --------------------------------------------------------------------------- #


def _decimal_or_none(value: object) -> Decimal | None:
    if isinstance(value, bool):
        raise DriverError("numeric field has a boolean value")
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text in _MISSING_TEXT:
            return None
        text = text.replace("$", "").replace(",", "")
        if text.startswith("(") and text.endswith(")"):
            text = f"-{text[1:-1]}"
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        raise DriverError("numeric field has an unsupported type")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise DriverError("numeric field is not decimal") from exc
    if not parsed.is_finite():
        raise DriverError("numeric field is not finite")
    return parsed


def _dec_text(value: Decimal) -> str:
    return format(value, "f")


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DriverError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DriverError(f"{name} must be a list")
    return [_object(item, name) for item in value]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DriverError(f"{name} must be nonempty text")
    return value.strip()


def _provider_date(value: object, name: str) -> date:
    text = _text(value, name)
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise DriverError(f"{name} has an unsupported date format")


def _as_of_fields(
    as_of: date, cutoff: Cutoff, exception_label: str | None
) -> dict[str, object]:
    offset = (as_of - cutoff.as_of_date).days
    if offset != 0 and exception_label is None:
        raise DriverError("as-of date differs from the declared cutoff close")
    return {
        "as_of_date": as_of.isoformat(),
        "as_of_exception_label": exception_label if offset != 0 else None,
        "as_of_offset_days": offset,
    }


def _as_of_basis(timing: CaptureTiming) -> str:
    if timing.captured_at >= timing.cutoff.close_utc:
        return "CAPTURED_AFTER_NEW_YORK_CLOSE_OF_AS_OF_DATE"
    return "CAPTURED_BEFORE_DECLARED_CLOSE_LABELLED"


def _row_key(item: Mapping[str, object]) -> tuple[str, str]:
    return (str(item["account_id"]), str(item["symbol"]))


def normalize_holdings(
    raw: object,
    *,
    crosswalk: Mapping[str, str],
    cutoff: Cutoff,
    captured_at: datetime,
    exception_label: str | None = None,
    max_lag_hours: int = DEFAULT_MAX_CAPTURE_LAG_HOURS,
) -> dict[str, object]:
    """Normalize one Holdings-V2 response; refuse null value on held positions."""

    label = _label(exception_label)
    timing = assess_timing(
        captured_at, cutoff, exception_label=label, max_lag_hours=max_lag_hours
    )
    payload = _object(raw, "holdings response")
    seen_accounts: set[str] = set()
    identities: set[tuple[str, str]] = set()
    positions: list[dict[str, object]] = []
    sweeps: list[dict[str, object]] = []
    zero_quantity_rows = 0
    refusals: dict[str, int] = {}

    def refuse(reason: str) -> None:
        refusals[reason] = refusals.get(reason, 0) + 1

    for account in _objects(payload.get("accounts"), "holdings accounts"):
        external = _text(account.get("accountId"), "holdings account")
        if external not in crosswalk:
            raise DriverError("holdings account is outside the crosswalk")
        if external in seen_accounts:
            raise DriverError("holdings account is duplicated")
        seen_accounts.add(external)
        internal = crosswalk[external]
        for group in _objects(account.get("groupedPositions"), "holdings groups"):
            group_type = group.get("securityType")
            if isinstance(group_type, bool) or not isinstance(group_type, int):
                raise DriverError("holdings group is unsupported")
            kind = _SWEEP_GROUP_KINDS.get(
                (_text(group.get("groupName"), "holdings group").casefold(), group_type)
            )
            if kind is None:
                raise DriverError("holdings group is unsupported")
            for row in _objects(group.get("holdingsRows"), "holdings rows"):
                symbol_value = row.get("dataSymbol")
                if not isinstance(symbol_value, str) or not symbol_value.strip():
                    symbol_value = _object(row.get("symbol"), "holdings symbol").get(
                        "symbol"
                    )
                symbol = _text(symbol_value, "holdings symbol")
                identity = (internal, symbol)
                if identity in identities:
                    raise DriverError("holdings identity is duplicated")
                identities.add(identity)
                quantity = _decimal_or_none(
                    _object(row.get("qty"), "holdings quantity").get("qty")
                )
                if quantity is None:
                    refuse("quantity_missing")
                    continue
                if quantity == 0:
                    zero_quantity_rows += 1
                    continue
                value_object = row.get("marketValue")
                market_value = _decimal_or_none(
                    _object(value_object, "holdings market value").get("val")
                    if value_object is not None
                    else None
                )
                if market_value is None:
                    refuse("market_value_null_for_held_position")
                    continue
                if row.get("priceDate") is None:
                    refuse("as_of_missing")
                    continue
                as_of = _provider_date(row.get("priceDate"), "holdings price date")
                try:
                    as_of_fields = _as_of_fields(as_of, cutoff, label)
                except DriverError:
                    refuse("as_of_differs_from_cutoff_close")
                    continue
                cost_object = row.get("costBasis")
                cost = (
                    _decimal_or_none(
                        _object(cost_object, "holdings cost basis").get("cstBasis")
                    )
                    if cost_object is not None
                    else None
                )
                record: dict[str, object] = {
                    "account_id": internal,
                    "cost_basis_usd": None if cost is None else _dec_text(cost),
                    "market_value_usd": _dec_text(market_value),
                    "quantity": _dec_text(quantity),
                    "symbol": symbol,
                    **as_of_fields,
                }
                record["record_id"] = (
                    "position-"
                    + _sha256(
                        _canonical(
                            {
                                "account_id": internal,
                                "as_of_date": as_of.isoformat(),
                                "symbol": symbol,
                            }
                        )
                    )[:24]
                )
                (positions if kind == "position" else sweeps).append(record)
    if refusals:
        raise DriverError(
            "holdings refused: "
            + ", ".join(
                f"{reason}={count}" for reason, count in sorted(refusals.items())
            )
        )
    if seen_accounts != set(crosswalk):
        raise DriverError("holdings account scope differs from the crosswalk")
    if not positions:
        raise DriverError("holdings response contains no held positions")
    positions.sort(key=_row_key)
    sweeps.sort(key=_row_key)
    return {
        "accounts": sorted(set(crosswalk.values())),
        "as_of_basis": _as_of_basis(timing),
        "capture": timing.to_dict(),
        "cutoff": cutoff.to_dict(),
        "exception_label": label,
        "normalizer_version": NORMALIZER_VERSION,
        "positions": positions,
        "provider": "SCHWAB",
        "schema_name": "schwab-capture-positions",
        "schema_version": SCHEMA_VERSION,
        "sweep_vehicles": sweeps,
        "unverified_shapes": list(UNVERIFIED_SHAPES),
        "zero_quantity_rows_omitted": zero_quantity_rows,
    }


def normalize_balances(
    raw_by_account: Mapping[str, object],
    *,
    cutoff: Cutoff,
    timing: CaptureTiming,
    exception_label: str | None = None,
) -> dict[str, object]:
    """Select the cutoff-dated balance per account (response shape UNVERIFIED)."""

    label = _label(exception_label)
    rows: list[dict[str, object]] = []
    for internal in sorted(raw_by_account):
        if _ACCOUNT_ID.fullmatch(internal) is None:
            raise DriverError("balance account id is invalid")
        document = _object(raw_by_account[internal], "balance response")
        observed: dict[date, Decimal] = {}
        for item in _objects(document.get("historicalBalances"), "balance rows"):
            day = _provider_date(item.get("date"), "balance date")
            value = _decimal_or_none(item.get("accountValue"))
            if value is None:
                raise DriverError("balance value is null")
            if day in observed and observed[day] != value:
                raise DriverError("balance date is conflicting")
            observed[day] = value
        if not observed:
            raise DriverError("balance response has no rows")
        selected = cutoff.as_of_date if cutoff.as_of_date in observed else max(observed)
        rows.append(
            {
                "account_id": internal,
                "account_value_usd": _dec_text(observed[selected]),
                **_as_of_fields(selected, cutoff, label),
            }
        )
    if not rows:
        raise DriverError("no balance responses were supplied")
    return {
        "balances": rows,
        "capture": timing.to_dict(),
        "cutoff": cutoff.to_dict(),
        "exception_label": label,
        "normalizer_version": NORMALIZER_VERSION,
        "provider": "SCHWAB",
        "schema_name": "schwab-capture-balances",
        "schema_version": SCHEMA_VERSION,
        "unverified_shapes": list(UNVERIFIED_SHAPES),
    }


# --------------------------------------------------------------------------- #
# Governance gate (mirrors the Matrixport live-read gate)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ApprovedRequest:
    capability: str
    method: str
    path: str
    query: Mapping[str, str]


@dataclass
class LiveReadGovernance:
    """A consumed, single-use attestation with a pre-network reservation."""

    receipt_path: Path
    evidence_sha256: str
    cutoff: Cutoff
    exception_label: str | None
    account_count: int
    requests: tuple[ApprovedRequest, ...]
    max_requests: int
    review: dict[str, str]
    _used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def request_for(self, capability: str) -> ApprovedRequest:
        for item in self.requests:
            if item.capability == capability:
                return item
        raise GovernanceError("capability is not approved by live-read scope")

    def reserve(self, method: str, path: str) -> None:
        """Durably record one request before any network I/O is attempted."""

        with self._lock:
            if not any(
                item.method == method and item.path == path for item in self.requests
            ):
                raise GovernanceError("request is not approved by live-read scope")
            if self._used >= self.max_requests:
                raise GovernanceError("live-read request budget is exhausted")
            self._used += 1
            _durable_replace(
                self.receipt_path,
                _canonical(
                    {
                        "evidence_sha256": self.evidence_sha256,
                        "max_requests": self.max_requests,
                        "retry_count": 0,
                        "schema_version": SCHEMA_VERSION,
                        "used_requests": self._used,
                    }
                ),
            )

    @property
    def used_requests(self) -> int:
        return self._used


def _durable_replace(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise GovernanceError("live-read receipt could not be durably updated") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _strict_keys(value: dict[str, Any], keys: set[str], name: str) -> None:
    if set(value) != keys:
        raise GovernanceError(f"governance {name} has an invalid field set")


def _parse_requests(value: object) -> tuple[ApprovedRequest, ...]:
    entries = {entry.capability: entry for entry in catalog()}
    approved: list[ApprovedRequest] = []
    seen: set[str] = set()
    if not isinstance(value, list):
        raise GovernanceError("governance scope.requests must be a list")
    for item in value:
        row = _object(item, "scope request")
        _strict_keys(row, {"capability", "method", "path", "query"}, "scope request")
        capability = row["capability"]
        entry = entries.get(capability) if isinstance(capability, str) else None
        if entry is None or capability in seen:
            raise GovernanceError("governance scope names an unknown or repeated read")
        seen.add(str(capability))
        method, path = row["method"], row["path"]
        if method != entry.method or entry.safety_class != "read_only":
            raise GovernanceError("governance scope method is not the catalog method")
        if not isinstance(path, str) or _SAFE_PATH.fullmatch(path) is None:
            raise GovernanceError("governance scope path is invalid")
        if ".." in path:
            raise GovernanceError("governance scope path is invalid")
        if entry.path is not None:
            if path != entry.path:
                raise GovernanceError("governance scope path differs from the catalog")
        elif any(word in path.casefold() for word in _FORBIDDEN_PATH_WORDS):
            raise GovernanceError("governance scope path is not a read-only route")
        query = _object(row["query"], "scope query")
        if len(query) > 8 or not all(
            _SAFE_QUERY_KEY.fullmatch(k)
            and isinstance(v, str)
            and _SAFE_QUERY_VALUE.fullmatch(v)
            for k, v in query.items()
        ):
            raise GovernanceError("governance scope query is invalid")
        if method == "POST" and query:
            raise GovernanceError("governance scope POST carries no query")
        approved.append(
            ApprovedRequest(str(capability), str(method), path, dict(query))
        )
    if seen != set(entries):
        raise GovernanceError("governance scope must name every catalog read")
    return tuple(sorted(approved, key=lambda item: item.capability))


def authorize_live_read(
    evidence_path: Path,
    *,
    receipt_root: Path,
    now: Callable[[], datetime] | None = None,
) -> LiveReadGovernance:
    """Validate and consume one operator-supplied attestation (single use)."""

    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise GovernanceError("live-read governance evidence file is required")
    try:
        payload = evidence_path.read_bytes()
    except OSError as exc:
        raise GovernanceError("live-read governance evidence cannot be read") from exc
    if not payload or len(payload) > _MAX_EVIDENCE_BYTES:
        raise GovernanceError("live-read governance evidence has an invalid size")
    try:
        root = _object(
            strict_json_loads(payload, label="governance evidence"), "evidence"
        )
    except Exception as exc:
        raise GovernanceError(
            "live-read governance evidence must be strict JSON"
        ) from exc
    _strict_keys(
        root,
        {
            "authority",
            "catalog",
            "cutoff",
            "exception_label",
            "metering",
            "operation",
            "review",
            "schema_version",
            "scope",
            "terms",
        },
        "evidence",
    )
    if root["schema_version"] != SCHEMA_VERSION or root["operation"] != "capture":
        raise GovernanceError("unsupported live-read governance operation")
    try:
        cutoff_row = _object(root["cutoff"], "cutoff")
        _strict_keys(cutoff_row, {"as_of_date", "close_time_zone"}, "cutoff")
        if cutoff_row["close_time_zone"] != CLOSE_TIME_ZONE:
            raise GovernanceError("governance close time zone is unsupported")
        cutoff = parse_cutoff(cutoff_row["as_of_date"])
        label = _label(root["exception_label"])
    except GovernanceError:
        raise
    except DriverError as exc:
        raise GovernanceError(str(exc)) from exc
    scope = _object(root["scope"], "scope")
    _strict_keys(scope, {"account_count", "requests"}, "scope")
    account_count = scope["account_count"]
    if isinstance(account_count, bool) or not isinstance(account_count, int):
        raise GovernanceError("governance account_count must be an integer")
    if not 1 <= account_count <= 64:
        raise GovernanceError("governance account_count is out of range")
    requests = _parse_requests(scope["requests"])
    review = _object(root["review"], "review")
    _strict_keys(review, {"identity", "reference"}, "review")
    identity, reference = review["identity"], review["reference"]
    if (
        not isinstance(identity, str)
        or not identity.strip()
        or not isinstance(reference, str)
        or _REVIEW_REFERENCE.fullmatch(reference) is None
    ):
        raise GovernanceError("governance review is invalid")
    authority = _object(root["authority"], "authority")
    _strict_keys(authority, {"expires_at", "issued_at", "status"}, "authority")
    if authority["status"] != "CURRENT":
        raise GovernanceError("live-read authority is not CURRENT")
    try:
        issued = _aware_utc(authority["issued_at"], "authority issued_at")
        expires = _aware_utc(authority["expires_at"], "authority expires_at")
    except DriverError as exc:
        raise GovernanceError(str(exc)) from exc
    current = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    if issued > current:
        raise GovernanceError("live-read authority is not yet current")
    if expires <= current:
        raise GovernanceError("live-read authority is expired")
    terms = _object(root["terms"], "terms")
    _strict_keys(terms, {"sha256", "version"}, "terms")
    if terms["version"] != TERMS_VERSION or terms["sha256"] != TERMS_SHA256:
        raise GovernanceError("live-read governance terms do not match accepted terms")
    catalog_row = _object(root["catalog"], "catalog")
    _strict_keys(catalog_row, {"sha256"}, "catalog")
    if catalog_row["sha256"] != catalog_sha256():
        raise GovernanceError("live-read governance catalog does not match this driver")
    metering = _object(root["metering"], "metering")
    _strict_keys(metering, {"max_requests", "retry_count"}, "metering")
    max_requests = metering["max_requests"]
    if isinstance(max_requests, bool) or not isinstance(max_requests, int):
        raise GovernanceError("governance max_requests must be an integer")
    if not 1 <= max_requests <= _MAX_REQUESTS or metering["retry_count"] != 0:
        raise GovernanceError("governance metering is outside the allowed range")

    evidence_sha256 = _sha256(payload)
    receipt_root.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_root / f"{evidence_sha256}.json"
    try:
        descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(
                _canonical(
                    {
                        "evidence_sha256": evidence_sha256,
                        "max_requests": max_requests,
                        "retry_count": 0,
                        "schema_version": SCHEMA_VERSION,
                        "used_requests": 0,
                    }
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise GovernanceError(
            "live-read governance evidence is already consumed"
        ) from exc
    except OSError as exc:
        raise GovernanceError(
            "live-read governance receipt could not be persisted"
        ) from exc
    return LiveReadGovernance(
        receipt_path=receipt_path,
        evidence_sha256=evidence_sha256,
        cutoff=cutoff,
        exception_label=label,
        account_count=account_count,
        requests=requests,
        max_requests=max_requests,
        review={"identity": identity.strip(), "reference": reference},
    )


# --------------------------------------------------------------------------- #
# Transport (governed) and the live CDP transport (never exercised by tests)
# --------------------------------------------------------------------------- #


class Transport(Protocol):
    def get(
        self, path: str, query: Mapping[str, str], provider_account_id: str | None
    ) -> tuple[int, object]: ...

    def post(
        self, path: str, body: Mapping[str, object], provider_account_id: str
    ) -> tuple[int, object]: ...


class GovernedTransport:
    """Reserves every request against the governance gate before delegating."""

    def __init__(self, inner: Transport, governance: LiveReadGovernance) -> None:
        self._inner = inner
        self._governance = governance

    def get(
        self, path: str, query: Mapping[str, str], provider_account_id: str | None
    ) -> tuple[int, object]:
        self._governance.reserve("GET", path)
        return self._inner.get(path, query, provider_account_id)

    def post(
        self, path: str, body: Mapping[str, object], provider_account_id: str
    ) -> tuple[int, object]:
        self._governance.reserve("POST", path)
        return self._inner.post(path, body, provider_account_id)


_ORIGIN = "https://ausgateway.schwab.com"
_POSITIONS_PAGE = "https://client.schwab.com/app/accounts/positions/"


class CdpTransport:  # pragma: no cover - live only, never run by the repository tests
    """Replays the owner's authenticated SPA headers for read-only requests."""

    def __init__(self, context: Any, observed_headers: Mapping[str, str]) -> None:
        self._context = context
        self._headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://client.schwab.com/",
            **observed_headers,
        }

    def _scoped(self, provider_account_id: str | None) -> dict[str, str]:
        if provider_account_id is None:
            return dict(self._headers)
        return {**self._headers, "Schwab-Client-Ids": provider_account_id}

    @staticmethod
    def _decode(response: Any) -> tuple[int, object]:
        try:
            value = response.json()
        except Exception as exc:
            raise DriverError("Schwab response is not JSON") from exc
        return int(response.status), value

    def get(
        self, path: str, query: Mapping[str, str], provider_account_id: str | None
    ) -> tuple[int, object]:
        response = self._context.request.get(
            _ORIGIN + path,
            headers=self._scoped(provider_account_id),
            params=dict(query),
        )
        return self._decode(response)

    def post(
        self, path: str, body: Mapping[str, object], provider_account_id: str
    ) -> tuple[int, object]:
        response = self._context.request.post(
            _ORIGIN + path,
            headers=self._scoped(provider_account_id),
            data=json.dumps(body, separators=(",", ":")),
        )
        return self._decode(response)


def _observe_headers(page: Any) -> dict[str, str]:  # pragma: no cover - live only
    def is_api(request: Any) -> bool:
        return (
            request.method == "GET"
            and request.url.startswith(_ORIGIN + "/")
            and "/api/" in request.url
        )

    with page.expect_request(is_api) as pending:
        if page.url.startswith("https://client.schwab.com/"):
            page.reload(wait_until="domcontentloaded")
        else:
            page.goto(_POSITIONS_PAGE, wait_until="domcontentloaded")
    observed = {key.casefold(): value for key, value in pending.value.headers.items()}
    chosen = {
        name: observed[name] for name in _OBSERVED_HEADER_NAMES if observed.get(name)
    }
    if "authorization" not in chosen:
        raise DriverError("authenticated Schwab SPA request was not observed")
    canonical_case = {
        "authorization": "Authorization",
        "correlatorid": "CorrelatorID",
        "schwab-channelcode": "Schwab-ChannelCode",
        "schwab-client-appid": "Schwab-Client-AppId",
        "schwab-client-channel": "Schwab-Client-Channel",
        "schwab-client-correlid": "Schwab-Client-CorrelId",
        "schwab-clientapp-name": "Schwab-ClientApp-Name",
        "schwab-env": "Schwab-Env",
        "schwab-environment": "Schwab-Environment",
        "schwab-resource-version": "Schwab-Resource-Version",
    }
    return {canonical_case[name]: value for name, value in chosen.items()}


# --------------------------------------------------------------------------- #
# Raw staging, run, manifest
# --------------------------------------------------------------------------- #


def _write_new(
    root: Path, relative: str, payload: bytes, role: str
) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise DriverError("staged artifact already exists") from exc
    return {
        "path": relative,
        "role": role,
        "sha256": _sha256(payload),
        "size_bytes": len(payload),
    }


def _single_ok(status: int, name: str) -> None:
    if status != 200:
        raise DriverError(f"{name} request failed with status {status}")


def run_capture(
    *,
    transport: Transport,
    governance: LiveReadGovernance,
    crosswalk: Mapping[str, str],
    targets: Sequence[TargetLike],
    cutoff: Cutoff,
    staging_root: Path,
    run_id: str,
    clock: Callable[[], datetime] | None = None,
    live_exercised: bool = False,
) -> dict[str, object]:
    """Capture holdings, balances and transactions under one governed run."""

    now = clock or (lambda: datetime.now(timezone.utc))
    if _RUN_ID.fullmatch(run_id) is None:
        raise DriverError("run id is invalid")
    if governance.cutoff != cutoff:
        raise GovernanceError("requested cutoff differs from the approved cutoff")
    if governance.account_count != len(crosswalk):
        raise GovernanceError("crosswalk size differs from the approved account count")
    if {t.provider_account_id: t.internal_account_id for t in targets} != dict(
        crosswalk
    ):
        raise DriverError("retained transaction targets differ from the crosswalk")
    label = governance.exception_label
    run_dir = staging_root / run_id
    if run_dir.exists():
        raise DriverError("run directory already exists")
    run_dir.mkdir(parents=True)
    started = now()
    governed = GovernedTransport(transport, governance)
    artifacts: list[dict[str, object]] = []
    requests: list[dict[str, object]] = []

    holdings_req = governance.request_for("holdings.v2")
    status, holdings_raw = governed.get(holdings_req.path, holdings_req.query, None)
    holdings_at = now()
    _single_ok(status, "holdings")
    artifacts.append(
        _write_new(run_dir, "raw/holdings.v2.json", _canonical(holdings_raw), "RAW")
    )
    requests.append({"capability": "holdings.v2", "status": status})

    balances_req = governance.request_for("balances.historical")
    balances_raw: dict[str, object] = {}
    for external, internal in sorted(crosswalk.items(), key=lambda item: item[1]):
        status, value = governed.get(balances_req.path, balances_req.query, external)
        _single_ok(status, "balances")
        balances_raw[internal] = value
        artifacts.append(
            _write_new(
                run_dir,
                f"raw/balances.historical/{internal}.json",
                _canonical(value),
                "RAW",
            )
        )
        requests.append(
            {
                "account_id": internal,
                "capability": "balances.historical",
                "status": status,
            }
        )
    balances_at = now()

    positions = normalize_holdings(
        holdings_raw,
        crosswalk=crosswalk,
        cutoff=cutoff,
        captured_at=holdings_at,
        exception_label=label,
    )
    timing = assess_timing(balances_at, cutoff, exception_label=label)
    balances = normalize_balances(
        balances_raw, cutoff=cutoff, timing=timing, exception_label=label
    )
    artifacts.append(
        _write_new(
            run_dir, "normalized/positions.json", _canonical(positions), "NORMALIZED"
        )
    )
    artifacts.append(
        _write_new(
            run_dir, "normalized/balances.json", _canonical(balances), "NORMALIZED"
        )
    )

    transactions_req = governance.request_for("transactions.history")

    def request_page(
        provider_account_id: str, body: dict[str, object]
    ) -> tuple[int, object]:
        return governed.post(transactions_req.path, body, provider_account_id)

    transactions_manifest = _TRANSACTION_CAPTURE.capture_transaction_history(
        request_page=request_page,
        targets=targets,
        output_root=run_dir / "transactions",
    )
    tx_summary = _bind_transactions(run_dir, transactions_manifest, cutoff, artifacts)
    requests.append(
        {"capability": "transactions.history", "page_requests": tx_summary["pages"]}
    )
    finished = now()

    manifest: dict[str, object] = {
        "accounts": sorted(set(crosswalk.values())),
        "artifacts": sorted(artifacts, key=lambda item: str(item["path"])),
        "catalog_sha256": catalog_sha256(),
        "cutoff": cutoff.to_dict(),
        "driver_version": DRIVER_VERSION,
        "evidence_sha256": governance.evidence_sha256,
        "exception_label": label,
        "finished_at_utc": finished.astimezone(timezone.utc).isoformat(),
        "live_exercised": live_exercised,
        "normalizer_version": NORMALIZER_VERSION,
        "provider": "SCHWAB",
        "request_count": governance.used_requests,
        "requests": requests,
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        "started_at_utc": started.astimezone(timezone.utc).isoformat(),
        "terms_sha256": TERMS_SHA256,
        "transactions": tx_summary["accounts"],
        "unverified_shapes": list(UNVERIFIED_SHAPES),
    }
    _write_new(run_dir, "manifest.json", _canonical(manifest), "MANIFEST")
    return manifest


def _txn_date(value: object) -> date:
    return _provider_date(value, "transaction date")


def _bind_transactions(
    run_dir: Path,
    child: Mapping[str, object],
    cutoff: Cutoff,
    artifacts: list[dict[str, object]],
) -> dict[str, Any]:
    """Hash-bind the existing transaction capture's files into this manifest."""

    child_accounts = _object(child.get("accounts"), "transaction accounts")
    summary: dict[str, dict[str, object]] = {}
    pages = 0
    for internal in sorted(child_accounts):
        state = _object(child_accounts[internal], "transaction account state")
        if state.get("terminal") is not True:
            raise DriverError("transaction history is not terminal")
        pages += int(state["page_count"])
        normalized = (
            run_dir
            / "transactions"
            / "normalized"
            / "wch"
            / "transactions"
            / f"{internal}.json"
        )
        rows = strict_json_loads(normalized.read_bytes(), label="transactions")
        if not isinstance(rows, list):
            raise DriverError("transactions must be a list")
        dates = [
            _txn_date(_object(row, "transaction").get("transactionDate"))
            for row in rows
        ]
        summary[internal] = {
            "after_cutoff_count": sum(day > cutoff.as_of_date for day in dates),
            "count": len(rows),
            "latest_date": max(dates).isoformat() if dates else None,
            "terminal": True,
        }
    for entry in _objects(child.get("artifacts"), "transaction artifacts"):
        artifacts.append(
            {
                "path": "transactions/" + str(entry["path"]),
                "role": "TRANSACTIONS",
                "sha256": entry["sha256"],
                "size_bytes": entry["size_bytes"],
            }
        )
    manifest_bytes = (run_dir / "transactions" / "manifest.json").read_bytes()
    artifacts.append(
        {
            "path": "transactions/manifest.json",
            "role": "TRANSACTIONS",
            "sha256": _sha256(manifest_bytes),
            "size_bytes": len(manifest_bytes),
        }
    )
    return {"accounts": summary, "pages": pages}


# --------------------------------------------------------------------------- #
# Independent verifier
# --------------------------------------------------------------------------- #


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        return _object(strict_json_loads(path.read_bytes(), label=label), label)
    except (OSError, DriverError) as exc:
        raise DriverError(f"{label} is unreadable or invalid") from exc
    except Exception as exc:
        raise DriverError(f"{label} is not strict JSON") from exc


def _check_as_of(
    row: Mapping[str, Any], cutoff: date, label: object, name: str
) -> None:
    offset = (date.fromisoformat(str(row["as_of_date"])) - cutoff).days
    if row.get("as_of_offset_days") != offset:
        raise DriverError(f"{name} offset is inconsistent")
    if offset != 0 and (label is None or row.get("as_of_exception_label") != label):
        raise DriverError(f"{name} differs from the cutoff without its label")
    if offset == 0 and row.get("as_of_exception_label") is not None:
        raise DriverError(f"{name} carries a label without an offset")


def verify_run(
    run_dir: Path, *, crosswalk: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Re-check a staged run from its bytes; raises DriverError on any failure."""

    try:
        return _verify_run(run_dir, crosswalk)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        if isinstance(exc, DriverError):
            raise
        raise DriverError("run contents are malformed") from exc


def _verify_run(
    run_dir: Path, crosswalk: Mapping[str, str] | None
) -> dict[str, object]:

    manifest = _load(run_dir / "manifest.json", "manifest")
    _strict_keys_driver(
        manifest,
        {
            "accounts", "artifacts", "catalog_sha256", "cutoff", "driver_version",
            "evidence_sha256", "exception_label", "finished_at_utc", "live_exercised",
            "normalizer_version", "provider", "request_count", "requests", "run_id",
            "schema_version", "started_at_utc", "terms_sha256", "transactions",
            "unverified_shapes",
        },
    )  # fmt: skip
    if manifest["provider"] != "SCHWAB" or manifest["schema_version"] != SCHEMA_VERSION:
        raise DriverError("manifest identity is unsupported")
    if manifest["catalog_sha256"] != catalog_sha256():
        raise DriverError("manifest catalog differs from this driver")
    cutoff = parse_cutoff(_object(manifest["cutoff"], "cutoff").get("as_of_date"))
    label = _label(manifest["exception_label"])
    listed = _objects(manifest["artifacts"], "artifacts")
    expected_files = {"manifest.json"}
    for entry in listed:
        relative = _text(entry.get("path"), "artifact path")
        if relative.startswith(("/", "..")) or ".." in relative.split("/"):
            raise DriverError("artifact path is unsafe")
        path = run_dir / relative
        if not path.is_file() or path.is_symlink():
            raise DriverError("artifact is missing")
        payload = path.read_bytes()
        if _sha256(payload) != entry.get("sha256") or len(payload) != entry.get(
            "size_bytes"
        ):
            raise DriverError("artifact bytes differ from the manifest")
        expected_files.add(relative)
    present = {
        p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*") if p.is_file()
    }
    if present != expected_files:
        raise DriverError("run directory holds files the manifest does not bind")

    accounts = manifest["accounts"]
    positions = _load(run_dir / "normalized/positions.json", "positions")
    balances = _load(run_dir / "normalized/balances.json", "balances")
    if (
        positions.get("accounts") != accounts
        or positions.get("exception_label") != label
    ):
        raise DriverError("positions scope or label differs from the manifest")
    held = _objects(positions.get("positions"), "positions")
    if not held:
        raise DriverError("positions are empty")
    for row in held + _objects(positions.get("sweep_vehicles"), "sweep vehicles"):
        if row.get("account_id") not in accounts:
            raise DriverError("position account is outside the manifest scope")
        quantity = _decimal_or_none(row.get("quantity"))
        market_value = _decimal_or_none(row.get("market_value_usd"))
        if quantity is None or quantity == 0 or market_value is None:
            raise DriverError("held position lacks a quantity or market value")
        _check_as_of(row, cutoff.as_of_date, label, "position")
    capture = _object(positions.get("capture"), "capture")
    captured = _aware_utc(capture.get("captured_at_utc"), "captured_at_utc")
    if label is None and not (
        cutoff.close_utc
        <= captured
        <= cutoff.close_utc + timedelta(hours=DEFAULT_MAX_CAPTURE_LAG_HOURS)
    ):
        raise DriverError("positions capture time is outside the cutoff window")
    brows = _objects(balances.get("balances"), "balances")
    if sorted(str(row["account_id"]) for row in brows) != accounts:
        raise DriverError("balance accounts differ from the manifest scope")
    for brow in brows:
        if _decimal_or_none(brow.get("account_value_usd")) is None:
            raise DriverError("balance value is null")
        _check_as_of(brow, cutoff.as_of_date, label, "balance")
    transactions = _object(manifest["transactions"], "transactions")
    if sorted(transactions) != accounts or not all(
        _object(v, "transaction summary").get("terminal") is True
        for v in transactions.values()
    ):
        raise DriverError("transaction history is not terminal for every account")
    if crosswalk is not None:
        blob = b"".join(
            (run_dir / name).read_bytes()
            for name in sorted(expected_files)
            if not name.startswith("raw/")
        )
        if any(external.encode("utf-8") in blob for external in crosswalk):
            raise DriverError("a provider account id appears in non-raw output")
        raw = strict_json_loads(
            (run_dir / "raw/holdings.v2.json").read_bytes(), label="raw"
        )
        again = normalize_holdings(
            raw,
            crosswalk=crosswalk,
            cutoff=cutoff,
            captured_at=captured,
            exception_label=label,
        )
        if _canonical(again) != _canonical(positions):
            raise DriverError("positions do not reproduce from the raw response")
    return {
        "account_count": len(accounts),
        "artifact_count": len(listed),
        "position_count": len(held),
        "status": "VERIFIED",
    }


def _strict_keys_driver(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise DriverError("manifest has an invalid field set")


# --------------------------------------------------------------------------- #
# Live entry and CLI
# --------------------------------------------------------------------------- #


def capture_live(
    *,
    evidence: Path,
    receipt_root: Path,
    cdp_url: str,
    crosswalk_path: Path,
    selected_source_root: Path,
    staging_root: Path,
    run_id: str,
    cutoff: Cutoff,
) -> dict[str, object]:  # pragma: no cover - live only
    """Refuses without governance evidence; never called by the repository tests."""

    governance = authorize_live_read(evidence, receipt_root=receipt_root)
    crosswalk = parse_crosswalk(_load(crosswalk_path, "crosswalk"))
    targets = _TRANSACTION_CAPTURE.load_retained_targets(selected_source_root)
    try:
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError as exc:
        raise DriverError("Playwright is required for live Schwab capture") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise DriverError("attached browser has no default context")
        context = browser.contexts[0]
        pages = [
            p for p in context.pages if p.url.startswith("https://client.schwab.com/")
        ]
        if not pages:
            raise DriverError("open an authenticated Schwab page before capture")
        transport = CdpTransport(context, _observe_headers(pages[0]))
        return run_capture(
            transport=transport,
            governance=governance,
            crosswalk=crosswalk,
            targets=targets,
            cutoff=cutoff,
            staging_root=staging_root,
            run_id=run_id,
            live_exercised=True,
        )


def _normalize_offline(args: argparse.Namespace) -> dict[str, object]:
    crosswalk = parse_crosswalk(_load(args.crosswalk, "crosswalk"))
    raw = strict_json_loads(args.raw_response.read_bytes(), label="raw holdings")
    document = normalize_holdings(
        raw,
        crosswalk=crosswalk,
        cutoff=parse_cutoff(args.cutoff),
        captured_at=_aware_utc(args.captured_at, "captured_at"),
        exception_label=args.exception_label,
    )
    payload = _canonical(document)
    try:
        with args.output.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise DriverError("output already exists") from exc
    return {"position_count": len(_objects(document["positions"], "positions")),
            "sha256": _sha256(payload)}  # fmt: skip


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog", help="print the approved-read catalog identity")
    capture = sub.add_parser("capture", help="governed live read (needs evidence)")
    capture.add_argument("--evidence", required=True, type=Path)
    capture.add_argument("--receipt-root", required=True, type=Path)
    capture.add_argument("--cdp-url", required=True)
    capture.add_argument("--crosswalk", required=True, type=Path)
    capture.add_argument("--selected-source-root", required=True, type=Path)
    capture.add_argument("--staging-root", required=True, type=Path)
    capture.add_argument("--run-id", required=True)
    capture.add_argument("--cutoff", required=True)
    normalize = sub.add_parser(
        "normalize-holdings", help="offline: normalize one retained holdings response"
    )
    normalize.add_argument("raw_response", type=Path)
    normalize.add_argument("crosswalk", type=Path)
    normalize.add_argument("output", type=Path)
    normalize.add_argument("--cutoff", required=True)
    normalize.add_argument("--captured-at", required=True)
    normalize.add_argument("--exception-label")
    verify = sub.add_parser("verify", help="re-check a staged run from its bytes")
    verify.add_argument("run_dir", type=Path)
    verify.add_argument("--crosswalk", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "catalog":
            result: dict[str, object] = {
                "catalog_sha256": catalog_sha256(),
                "entries": [entry.row() for entry in catalog()],
                "terms_sha256": TERMS_SHA256,
            }
        elif args.command == "capture":  # pragma: no cover - live only
            result = capture_live(
                evidence=args.evidence,
                receipt_root=args.receipt_root,
                cdp_url=args.cdp_url,
                crosswalk_path=args.crosswalk,
                selected_source_root=args.selected_source_root,
                staging_root=args.staging_root,
                run_id=args.run_id,
                cutoff=parse_cutoff(args.cutoff),
            )
        elif args.command == "normalize-holdings":
            result = _normalize_offline(args)
        else:
            crosswalk = (
                parse_crosswalk(_load(args.crosswalk, "crosswalk"))
                if args.crosswalk
                else None
            )
            result = verify_run(args.run_dir, crosswalk=crosswalk)
    except (DriverError, SchwabCaptureError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
