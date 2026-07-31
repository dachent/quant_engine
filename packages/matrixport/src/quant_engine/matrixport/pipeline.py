"""Read-only Matrixport extraction orchestration.

The pipeline deliberately keeps authentication inside the transport boundary,
stores response bytes before parsing them into canonical rows, and never
constructs a request outside the endpoint catalog.
"""

from __future__ import annotations

import json
import re
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .catalog import ENDPOINTS, Endpoint, catalog_rows, get_endpoint
from .credentials import ApiCredentials, load_credentials
from .exporters import export_datasets, write_csv
from .normalization import enrich_timestamps, normalize_rows
from .pagination import DAY_MS, paginate_rfq_bisection, utc_time_windows
from .raw_store import RawStore
from .schema import DATASET_DESCRIPTIONS
from .storage import RunStorage
from .transport import ReadOnlyTransport, TransportError, TransportHTTPError
from .util import atomic_write, canonical_json, sha256_text, utc_now_ms, utc_run_id
from .valuation import build_daily_valuation


DEFAULT_CREDENTIAL_TARGET = "BIT.com-API"
OUTPUT_FOLDER = "output"
DATABASE_NAME = "matrixport.sqlite"
OFFSET_PAGE_SIZE = 50
RFQ_CAP = 100
FULL_START_MS = 0
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}\.\d{3}Z$")


class ExtractionError(RuntimeError):
    """A sanitized pipeline error that contains no authentication material."""


class ApiResponseError(ExtractionError):
    def __init__(self, endpoint: str, code: str) -> None:
        super().__init__(f"Matrixport API error at {endpoint} (code={code})")
        self.endpoint = endpoint
        self.code = code


@dataclass(frozen=True, slots=True)
class ApiCall:
    endpoint: Endpoint
    payload: Any
    request_id: str
    resumed: bool


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    run_id: str
    run_dir: Path
    status: str
    request_count: int
    dataset_counts: dict[str, int]


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ExtractionError("Invalid run ID")
    return run_id


def _json_loads(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ExtractionError("Matrixport returned invalid JSON") from None


def _api_code(payload: Any) -> str | None:
    if not isinstance(payload, Mapping) or "code" not in payload:
        return None
    code = payload.get("code")
    if code in (None, 0, "0", Decimal(0)):
        return None
    return str(code)


def _data(payload: Any) -> Any:
    return (
        payload.get("data")
        if isinstance(payload, Mapping) and "data" in payload
        else payload
    )


def _mapping_rows(value: Mapping[str, Any], *, key_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, item in value.items():
        if isinstance(item, Mapping):
            row = dict(item)
            row.setdefault(key_name, str(key))
        else:
            row = {key_name: str(key), "value": item}
        rows.append(row)
    return rows


def rows_from_payload(
    payload: Any,
    *,
    candidates: Sequence[str] = (
        "items",
        "records",
        "list",
        "rows",
        "bills",
        "currencies",
    ),
    mapping_key: str | None = None,
) -> list[dict[str, Any]]:
    value = _data(payload)
    if isinstance(value, list):
        return [
            dict(item) if isinstance(item, Mapping) else {"value": item}
            for item in value
        ]
    if not isinstance(value, Mapping):
        return [] if value is None else [{"value": value}]
    for candidate in candidates:
        if candidate not in value:
            continue
        items = value.get(candidate)
        if items is None:
            return []
        if isinstance(items, list):
            return [
                dict(item) if isinstance(item, Mapping) else {"value": item}
                for item in items
            ]
        if isinstance(items, Mapping) and mapping_key:
            return _mapping_rows(items, key_name=mapping_key)
    if (
        mapping_key
        and value
        and all(isinstance(item, Mapping) for item in value.values())
    ):
        return _mapping_rows(value, key_name=mapping_key)
    return [dict(value)] if value else []


def _row_count(payload: Any) -> int:
    value = _data(payload)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        lengths = [len(item) for item in value.values() if isinstance(item, list)]
        return sum(lengths) if lengths else (1 if value else 0)
    return 0 if value in (None, "") else 1


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def field_value(row: Mapping[str, Any], *names: str) -> Any:
    # Callers order alternatives from most to least specific.  Preserve that
    # order so a generic ``id`` cannot win merely because it appears before a
    # more meaningful ``product_id`` in the source object.
    for name in names:
        wanted = _normalized_name(name)
        for key, value in row.items():
            if _normalized_name(str(key).split(".")[-1]) == wanted and value not in (
                None,
                "",
            ):
                return value
    return None


STABLE_KEY_FIELDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "endpoint_catalog": (("family",), ("host",), ("method",), ("path",)),
    "products": (("product_id", "productId", "id"),),
    "product_catalog": (("product_id", "productId", "id"),),
    "product_translations": (("product_id",), ("language", "locale", "lang"), ("id",)),
    "product_allocations": (
        ("product_id",),
        ("currency", "asset", "symbol", "name"),
        ("id",),
    ),
    "product_fee_tiers": (("product_id",), ("tier", "level", "id"), ("min", "minimum")),
    "holdings": (("product_id", "productId", "id"),),
    "portfolio_snapshots": (
        ("timestamp", "time_stamp", "updated_at", "as_of"),
        ("id",),
    ),
    "strategy_trades": (
        ("order_id", "orderId", "id"),
        ("serial_no", "serialNumber", "sn"),
    ),
    "subscription_details": (
        ("order_id", "orderId", "id"),
        ("serial_no", "serialNumber", "sn"),
    ),
    "redemption_details": (
        ("order_id", "orderId", "id"),
        ("serial_no", "serialNumber", "sn"),
    ),
    "reservations": (("reservation_record_id", "id"), ("order_id",), ("product_id",)),
    "nav_daily": (
        ("product_id",),
        ("day_timestamp", "time_stamp", "timestamp_ms", "utc_date"),
    ),
    "nav_realtime": (
        ("product_id",),
        ("day_timestamp", "time_stamp", "timestamp_ms", "utc_date"),
    ),
    "btc_performance": (("product_id",), ("timestamp", "time_stamp", "timestamp_ms")),
    "wallet_balances": (("currency", "coin", "asset"),),
    "wallet_currencies": (
        ("currency", "coin", "asset", "symbol"),
        ("chain", "network"),
    ),
    "wallet_bills": (
        ("id", "bill_id", "serial_no", "transaction_id"),
        ("currency",),
        ("timestamp", "created_at"),
    ),
    "wallet_deposits": (
        ("id", "txid", "transaction_id", "order_id"),
        ("currency",),
        ("created_at", "timestamp"),
    ),
    "wallet_withdrawals": (
        ("id", "txid", "transaction_id", "order_id"),
        ("currency",),
        ("created_at", "timestamp"),
    ),
    "rfq_symbols": (("symbol", "pair", "instrument_id", "value"),),
    "rfq_orders": (("order_id", "client_order_id", "id"),),
    "prime_accounts": (("account_id", "id"),),
    "prime_balances": (("account_id", "id"), ("currency",)),
    "prime_flows": (("serial_no", "transfer_serial_no", "id"),),
    "prime_funds": (("serial_no", "id"),),
    "position_events": (
        ("product_id",),
        ("effective_ms",),
        ("event_type",),
        ("serial_number", "source_id"),
    ),
    "daily_holdings": (("utc_date",), ("product_id",)),
    "daily_currency_subtotals": (("utc_date",), ("currency",)),
    "daily_portfolio_totals": (("utc_date",),),
    "reconciliation": (("product_id",),),
    "coverage": (("endpoint",),),
    "request_manifest": (("request_hash",),),
    "request_page_audit": (("request_id",),),
    "data_dictionary": (("dataset",), ("column",)),
}


def stable_key(dataset: str, row: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for alternatives in STABLE_KEY_FIELDS.get(dataset, ()):
        value = field_value(row, *alternatives)
        if value not in (None, ""):
            parts.append(str(value))
    if parts:
        return sha256_text(canonical_json([dataset, *parts]))
    return sha256_text(canonical_json([dataset, dict(row)]))


def record_timestamp_ms(row: Mapping[str, Any]) -> int | None:
    preferred = (
        "day_timestamp",
        "time_stamp",
        "timestamp_ms",
        "timestamp_in_ms",
        "confirmation_time",
        "confirm_shares_time",
        "created_at",
        "updated_at",
        "start_time",
        "bill_time",
    )
    value = field_value(row, *preferred)
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
        if abs(number) < Decimal("100000000000"):
            number *= 1000
        parsed = int(number)
        return parsed if parsed > 0 else None
    except Exception:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return int(moment.timestamp() * 1000)
        except Exception:
            return None


def _extractor_hash() -> str:
    base = Path(__file__).resolve().parent
    material = []
    for path in sorted(base.glob("*.py")):
        material.append((path.name, sha256_text(path.read_text(encoding="utf-8"))))
    return sha256_text(canonical_json(material))


def _config_hash() -> str:
    return sha256_text(canonical_json(catalog_rows()))


class MatrixportExtractor:
    def __init__(
        self,
        workspace: str | Path,
        *,
        mode: str,
        run_id: str | None = None,
        resume: bool = False,
        credentials: ApiCredentials | None = None,
        transport: ReadOnlyTransport | None = None,
        reporter: Callable[[str], None] | None = None,
    ) -> None:
        if mode not in {"full", "incremental"}:
            raise ValueError("mode must be full or incremental")
        self.workspace = Path(workspace).resolve()
        self.output_root = self.workspace / OUTPUT_FOLDER
        self.run_id = validate_run_id(run_id) if run_id is not None else utc_run_id()
        self.run_dir = self.output_root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=resume)
        for child in ("raw", "state", "csv", "xlsx", "reports"):
            (self.run_dir / child).mkdir(parents=True, exist_ok=True)
        self.storage = RunStorage(self.run_dir / "state" / DATABASE_NAME)
        self.reporter = reporter or (lambda _message: None)
        self.extractor_hash = _extractor_hash()
        self.config_hash = _config_hash()
        self.quality_issues: list[dict[str, Any]] = []
        self.collections: dict[str, list[dict[str, Any]]] = {}
        self.previous_collections: dict[str, list[dict[str, Any]]] = {}

        existing = self.storage.get_run(self.run_id)
        if resume:
            if existing is None:
                raise ExtractionError(f"Cannot resume unknown run {self.run_id}")
            if existing["status"] != "RUNNING":
                raise ExtractionError(
                    f"Cannot resume {existing['status']} run {self.run_id}"
                )
            self.mode = str(existing["mode"])
            self.run_end_ms = int(existing["run_end_ms"])
            if str(existing["extractor_hash"]) != self.extractor_hash:
                raise ExtractionError("Cannot resume a run with changed extractor code")
            if str(existing["config_hash"]) != self.config_hash:
                raise ExtractionError(
                    "Cannot resume a run with a changed endpoint catalog"
                )
        else:
            if existing is not None:
                raise ExtractionError(f"Run already exists: {self.run_id}")
            self.mode = mode
            self.run_end_ms = utc_now_ms()
            self.storage.create_run(
                self.run_id,
                mode=mode,
                run_end_ms=self.run_end_ms,
                extractor_hash=self.extractor_hash,
                config_hash=self.config_hash,
                metadata={
                    "credential_target": DEFAULT_CREDENTIAL_TARGET,
                    "history_semantics": "all_api_reachable_history_to_frozen_run_end",
                    "timezone": "UTC",
                },
            )
        self.raw = RawStore(
            self.run_dir / "raw", storage=self.storage, run_id=self.run_id
        )
        self.transport = transport or ReadOnlyTransport(
            credentials or load_credentials()
        )
        self.previous_run_dir, self._previous_end_ms = self._find_previous_completed()
        if self.mode == "incremental" and self.previous_run_dir is not None:
            self._seed_previous_canonical(write=not resume)

    def close(self) -> None:
        self.storage.close()

    def _find_previous_completed(self) -> tuple[Path | None, int | None]:
        previous: list[tuple[int, Path]] = []
        if not self.output_root.exists():
            return None, None
        for directory in self.output_root.iterdir():
            database = directory / "state" / DATABASE_NAME
            if directory == self.run_dir or not database.is_file():
                continue
            verification_path = directory / "reports" / "verification.json"
            try:
                verification = json.loads(verification_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if verification.get("ok") is not True:
                continue
            try:
                with RunStorage(database) as storage:
                    row = storage.get_run(directory.name)
                    if row and row["status"] == "COMPLETED":
                        previous.append((int(row["run_end_ms"]), directory))
            except Exception:
                continue
        if not previous:
            return None, None
        run_end_ms, directory = max(previous, key=lambda item: item[0])
        return directory, run_end_ms

    def _seed_previous_canonical(self, *, write: bool) -> None:
        if self.previous_run_dir is None:
            return
        excluded = {
            "coverage",
            "data_dictionary",
            "endpoint_catalog",
            "incremental_lineage",
            "quality_checks",
            "request_manifest",
            "request_page_audit",
            "schema_drift",
            "valuation_quality_issues",
        }
        prior_id = self.previous_run_dir.name
        with RunStorage(self.previous_run_dir / "state" / DATABASE_NAME) as previous:
            for dataset in previous.dataset_names(prior_id):
                if dataset in excluded:
                    continue
                rows = [
                    dict(item["row"])
                    for item in previous.iter_canonical_rows(prior_id, dataset)
                ]
                self.previous_collections[dataset] = rows
                if not write:
                    continue
                seeded = []
                for row in rows:
                    item = dict(row)
                    item.setdefault("source_prior_run_id", prior_id)
                    seeded.append((stable_key(dataset, item), item, None))
                self.storage.upsert_canonical_rows(self.run_id, dataset, seeded)
        if not write:
            return
        self._store_rows(
            "incremental_lineage",
            [
                {
                    "base_run_id": prior_id,
                    "base_run_end_ms": self._previous_end_ms,
                    "policy": "copy_forward_then_overlay_30d_history_7d_nav",
                }
            ],
            request_id=None,
        )

    @property
    def history_start_ms(self) -> int:
        if self.mode == "incremental" and self._previous_end_ms is not None:
            return max(FULL_START_MS, self._previous_end_ms - 30 * DAY_MS)
        return FULL_START_MS

    @property
    def nav_start_ms(self) -> int:
        if self.mode == "incremental" and self._previous_end_ms is not None:
            return max(FULL_START_MS, self._previous_end_ms - 7 * DAY_MS)
        return FULL_START_MS

    def _initialize_coverage(self) -> None:
        for endpoint in ENDPOINTS:
            status = endpoint.disposition
            reason = "catalog_disposition"
            if endpoint.live_enabled and status == "complete_to_observed_boundary":
                status = "partial"
                reason = "not_attempted"
            if endpoint.name == "product_range_search":
                status = "unsupported"
                reason = "catalog_only_no_keyword"
            self.storage.set_coverage(
                self.run_id,
                f"{endpoint.method} https://{endpoint.host}{endpoint.path}",
                status,
                terminal_reason=reason,
                details={"catalog_name": endpoint.name, "family": endpoint.family},
            )

    def _coverage(
        self,
        endpoint: str,
        status: str,
        *,
        terminal_reason: str,
        requested: Mapping[str, Any] | None = None,
        observed: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        coverage_key = endpoint
        if endpoint.startswith("/"):
            catalog_endpoint = get_endpoint(endpoint)
            coverage_key = f"{catalog_endpoint.method} https://{catalog_endpoint.host}{catalog_endpoint.path}"
        self.storage.set_coverage(
            self.run_id,
            coverage_key,
            status,
            requested_bounds=requested,
            observed_bounds=observed,
            terminal_reason=terminal_reason,
            details=details,
        )
        if endpoint.startswith("/"):
            self.storage.set_checkpoint(
                self.run_id,
                endpoint,
                "coverage",
                {
                    "status": status,
                    "terminal_reason": terminal_reason,
                    "requested": dict(requested or {}),
                    "observed": dict(observed or {}),
                },
                done=status
                in {"complete_to_observed_boundary", "not_applicable", "unsupported"},
            )

    def _call(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        observed_bounds: Mapping[str, Any] | None = None,
    ) -> ApiCall:
        endpoint = get_endpoint(path)
        safe_params = {
            key: value for key, value in dict(params or {}).items() if value is not None
        }
        stored = self.raw.lookup(path, safe_params)
        if stored is not None:
            payload = _json_loads(stored.response_path.read_bytes())
            code = _api_code(payload)
            if code is not None:
                raise ApiResponseError(path, code)
            return ApiCall(endpoint, payload, stored.request_id, True)

        try:
            response = self.transport.get(path, params=safe_params)
        except TransportHTTPError as error:
            self.raw.stage_response(
                endpoint=path, safe_params=safe_params, response_bytes=error.content
            )
            try:
                error_payload = _json_loads(error.content)
                error_row_count = _row_count(error_payload)
            except ExtractionError:
                error_row_count = 0
            self.raw.store_response(
                endpoint=path,
                safe_params=safe_params,
                response_bytes=error.content,
                status_code=error.status_code,
                elapsed_ms=error.elapsed_ms,
                row_count=error_row_count,
                request_id=error.request_id,
                extractor_hash=self.extractor_hash,
                config_hash=self.config_hash,
                observed_bounds=observed_bounds,
                terminal_reason="http_error",
            )
            raise
        self.raw.stage_response(
            endpoint=path, safe_params=safe_params, response_bytes=response.content
        )
        try:
            payload = _json_loads(response.content)
        except ExtractionError:
            self.raw.store_response(
                endpoint=path,
                safe_params=safe_params,
                response_bytes=response.content,
                status_code=response.status_code,
                elapsed_ms=response.elapsed_ms,
                row_count=0,
                request_id=response.request_id,
                extractor_hash=self.extractor_hash,
                config_hash=self.config_hash,
                observed_bounds=observed_bounds,
                terminal_reason="invalid_json",
            )
            raise
        stored = self.raw.store_response(
            endpoint=path,
            safe_params=safe_params,
            response_bytes=response.content,
            status_code=response.status_code,
            elapsed_ms=response.elapsed_ms,
            row_count=_row_count(payload),
            request_id=response.request_id,
            extractor_hash=self.extractor_hash,
            config_hash=self.config_hash,
            observed_bounds=observed_bounds,
        )
        code = _api_code(payload)
        if code is not None:
            raise ApiResponseError(path, code)
        return ApiCall(endpoint, payload, stored.request_id, stored.resumed)

    def _safe_call(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        core: bool = False,
        observed_bounds: Mapping[str, Any] | None = None,
    ) -> ApiCall | None:
        try:
            return self._call(path, params, observed_bounds=observed_bounds)
        except (ApiResponseError, TransportError, ExtractionError) as error:
            self.quality_issues.append(
                {
                    "source_table": "request",
                    "endpoint": path,
                    "reason": type(error).__name__,
                    "detail": str(error),
                }
            )
            permission_denied = isinstance(
                error, TransportHTTPError
            ) and error.status_code in {401, 403}
            self._coverage(
                path,
                "permission_denied" if permission_denied else "partial",
                terminal_reason=type(error).__name__,
            )
            if core:
                raise
            return None

    def _store_rows(
        self,
        dataset: str,
        records: Iterable[Mapping[str, Any]],
        *,
        request_id: str | None,
        endpoint: str = "derived",
    ) -> list[dict[str, Any]]:
        materialized = [dict(item) for item in records]
        normalized = enrich_timestamps(normalize_rows(materialized))
        for row in normalized:
            row.setdefault("source_endpoint", endpoint)
            if request_id:
                row.setdefault("source_request_id", request_id)
        self.storage.upsert_canonical_rows(
            self.run_id,
            dataset,
            ((stable_key(dataset, row), row, request_id) for row in normalized),
        )
        self.collections.setdefault(dataset, []).extend(materialized)
        return normalized

    def _record_page_audit(
        self,
        call: ApiCall,
        params: Mapping[str, Any] | None,
        rows: Sequence[Mapping[str, Any]],
        *,
        terminal_reason: str,
    ) -> None:
        safe_params = dict(params or {})
        requested = {
            key: value
            for key, value in safe_params.items()
            if any(
                marker in key.lower() for marker in ("start", "end", "offset", "limit")
            )
        }
        observed_timestamps = sorted(
            timestamp
            for row in rows
            if (timestamp := record_timestamp_ms(row)) is not None
        )
        audit = {
            "request_id": call.request_id,
            "endpoint": call.endpoint.path,
            "requested_bounds": canonical_json(requested),
            "observed_start_ms": observed_timestamps[0] if observed_timestamps else "",
            "observed_end_ms": observed_timestamps[-1] if observed_timestamps else "",
            "raw_row_count": len(rows),
            "terminal_reason": terminal_reason,
            "resumed": str(call.resumed).lower(),
        }
        self._store_rows(
            "request_page_audit",
            [audit],
            request_id=call.request_id,
            endpoint=call.endpoint.path,
        )
        self.storage.set_checkpoint(
            self.run_id,
            call.endpoint.path,
            "latest",
            audit,
            done=terminal_reason
            not in {"continue", "window_response", "capped_window"},
        )

    def _single(
        self,
        path: str,
        dataset: str,
        *,
        params: Mapping[str, Any] | None = None,
        candidates: Sequence[str] = ("items", "records", "list", "rows"),
        mapping_key: str | None = None,
        core: bool = False,
    ) -> list[dict[str, Any]]:
        call = self._safe_call(path, params, core=core)
        if call is None:
            return []
        rows = rows_from_payload(
            call.payload, candidates=candidates, mapping_key=mapping_key
        )
        self._record_page_audit(call, params, rows, terminal_reason="single_response")
        self._store_rows(dataset, rows, request_id=call.request_id, endpoint=path)
        self._coverage(
            path,
            "complete_to_observed_boundary",
            terminal_reason="single_response",
            details={"row_count": len(rows)},
        )
        return rows

    def _offset(
        self,
        path: str,
        dataset: str,
        *,
        base_params: Mapping[str, Any] | None = None,
        candidates: Sequence[str] = ("items", "records", "list", "rows"),
        core: bool = False,
        set_coverage: bool = True,
        page_size: int = OFFSET_PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        output: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        terminal = "max_pages"
        complete = False
        for page_number in range(10_000):
            offset = page_number * page_size
            params = dict(base_params or {})
            params.update({"limit": page_size, "offset": offset})
            call = self._safe_call(path, params, core=core)
            if call is None:
                terminal = "request_failed"
                break
            rows = rows_from_payload(call.payload, candidates=candidates)
            signature = sha256_text(canonical_json(rows))
            if rows and signature in seen_pages:
                terminal = "repeated_page"
                self._record_page_audit(call, params, rows, terminal_reason=terminal)
                break
            seen_pages.add(signature)
            self._store_rows(dataset, rows, request_id=call.request_id, endpoint=path)
            output.extend(rows)
            if not rows:
                terminal = "empty_page"
                complete = True
                self._record_page_audit(call, params, rows, terminal_reason=terminal)
                break
            if len(rows) < page_size:
                terminal = "short_page"
                complete = True
                self._record_page_audit(call, params, rows, terminal_reason=terminal)
                break
            self._record_page_audit(call, params, rows, terminal_reason="continue")
        if set_coverage:
            self._coverage(
                path,
                "complete_to_observed_boundary" if complete else "partial",
                terminal_reason=terminal,
                details={"raw_rows": len(output), "page_size": page_size},
            )
        return output, terminal, complete

    def _strategy_history_fallback(
        self,
        path: str,
        dataset: str,
        *,
        start_param: str,
        end_param: str,
        base_params: Mapping[str, Any] | None = None,
        candidates: Sequence[str] = ("items", "records", "list", "rows"),
    ) -> tuple[list[dict[str, Any]], bool]:
        """Use 90-day windows if a strategy offset walk could not terminate naturally."""
        complete = True
        output: list[dict[str, Any]] = []
        for window in utc_time_windows(
            self.history_start_ms,
            self.run_end_ms,
            window_days=90,
            overlap_days=1,
        ):
            params = dict(base_params or {})
            params.update({start_param: window.start_ms, end_param: window.end_ms})
            rows, _terminal, window_complete = self._offset(
                path,
                dataset,
                base_params=params,
                candidates=candidates,
                set_coverage=False,
            )
            output.extend(rows)
            complete = complete and window_complete
        self._coverage(
            path,
            "complete_to_observed_boundary" if complete else "partial",
            terminal_reason="90_day_window_fallback",
            requested={"start_ms": self.history_start_ms, "end_ms": self.run_end_ms},
        )
        deduplicated: dict[str, dict[str, Any]] = {}
        for row in output:
            deduplicated[stable_key(dataset, row)] = row
        return list(deduplicated.values()), complete

    def _extract_product_children(
        self, product_id: str, record: Mapping[str, Any], request_id: str
    ) -> None:
        targets: dict[str, tuple[str, ...]] = {
            "product_translations": ("translation", "translate", "locale", "language"),
            "product_allocations": (
                "allocation",
                "distribution",
                "composition",
                "asset",
            ),
            "product_fee_tiers": ("fee", "tier", "rate"),
        }

        def walk(value: Any, path: tuple[str, ...] = ()) -> None:
            if isinstance(value, Mapping):
                label = ".".join(path).lower()
                if path and path[-1].lower() in {
                    "holdings",
                    "product_fee_structure_v2",
                }:
                    dataset = (
                        "product_allocations"
                        if path[-1].lower() == "holdings"
                        else "product_fee_tiers"
                    )
                    self._store_rows(
                        dataset,
                        [
                            {
                                **dict(value),
                                "product_id": product_id,
                                "source_path": label,
                            }
                        ],
                        request_id=request_id,
                        endpoint="/strategy/api/v2/product",
                    )
                for key, item in value.items():
                    walk(item, (*path, str(key)))
            elif isinstance(value, list):
                label = ".".join(path).lower()
                language_rows = bool(
                    value
                    and isinstance(value[0], Mapping)
                    and field_value(value[0], "language", "locale", "lang")
                    not in (None, "")
                )
                targets_for_list: dict[str, tuple[str, ...]]
                if language_rows:
                    targets_for_list = {"product_translations": ("language",)}
                else:
                    targets_for_list = targets
                for dataset, terms in targets_for_list.items():
                    if any(term in label for term in terms):
                        rows = []
                        for index, item in enumerate(value):
                            row = (
                                dict(item)
                                if isinstance(item, Mapping)
                                else {"value": item}
                            )
                            row.update(
                                {
                                    "product_id": product_id,
                                    "source_path": ".".join(path),
                                    "source_index": index,
                                }
                            )
                            rows.append(row)
                        self._store_rows(
                            dataset,
                            rows,
                            request_id=request_id,
                            endpoint="/strategy/api/v2/product",
                        )
                        break
                for item in value:
                    if isinstance(item, (Mapping, list)):
                        walk(item, path)

        walk(record)

    def _prepare_nav(
        self, product_id: str, row: Mapping[str, Any], nav_type: str
    ) -> dict[str, Any]:
        item = dict(row)
        item.setdefault("product_id", product_id)
        item["nav_type"] = nav_type
        timestamp = field_value(
            item, "day_timestamp", "time_stamp", "timestamp_ms", "timestamp", "time"
        )
        if timestamp not in (None, ""):
            item.setdefault("timestamp_ms", timestamp)
        return item

    def _extract_strategy(self) -> None:
        self.reporter("strategy: current holdings and history")
        holdings = self._single(
            "/strategy/api/v2/user/holding",
            "holdings",
            candidates=("items", "holdings", "records", "list"),
            core=True,
        )
        self._single(
            "/strategy/api/v2/user/holding/volume",
            "portfolio_snapshots",
            candidates=("items", "records", "list"),
            core=True,
        )
        trades, terminal, complete = self._offset(
            "/strategy/api/v2/user/trade_record",
            "strategy_trades",
            base_params={
                "start_time": self.history_start_ms,
                "end_time": self.run_end_ms,
            },
            candidates=("items", "records", "trade_records", "list"),
            core=True,
        )
        if not complete and terminal in {"repeated_page", "max_pages"}:
            fallback_rows, _fallback_complete = self._strategy_history_fallback(
                "/strategy/api/v2/user/trade_record",
                "strategy_trades",
                start_param="start_time",
                end_param="end_time",
                candidates=("items", "records", "trade_records", "list"),
            )
            combined = {
                stable_key("strategy_trades", row): row
                for row in [*trades, *fallback_rows]
            }
            trades = list(combined.values())
        prior_trades = self.previous_collections.get("strategy_trades", [])
        if prior_trades:
            combined = {
                stable_key("strategy_trades", row): row
                for row in [*prior_trades, *trades]
            }
            trades = list(combined.values())
        self._offset(
            "/strategy/api/v2/products",
            "product_catalog",
            candidates=("items", "products", "records", "list"),
        )

        product_ids = {
            str(value)
            for row in [*holdings, *trades]
            if (value := field_value(row, "product_id", "productId", "id"))
            not in (None, "")
        }
        product_records: list[dict[str, Any]] = list(
            self.previous_collections.get("products", [])
        )
        for product_id in sorted(product_ids):
            call = self._safe_call(
                "/strategy/api/v2/product", {"product_id": product_id}, core=True
            )
            if call is None:
                continue
            records = rows_from_payload(
                call.payload, candidates=("items", "products", "records")
            )
            self._record_page_audit(
                call,
                {"product_id": product_id},
                records,
                terminal_reason="single_response",
            )
            for record in records:
                record.setdefault("product_id", product_id)
                self._extract_product_children(product_id, record, call.request_id)
            self._store_rows(
                "products",
                records,
                request_id=call.request_id,
                endpoint="/strategy/api/v2/product",
            )
            product_records.extend(records)
        self._coverage(
            "/strategy/api/v2/product",
            "complete_to_observed_boundary",
            terminal_reason="all_discovered_product_ids",
            details={"product_count": len(product_ids)},
        )

        subscriptions: list[dict[str, Any]] = list(
            self.previous_collections.get("subscription_details", [])
        )
        redemptions: list[dict[str, Any]] = list(
            self.previous_collections.get("redemption_details", [])
        )
        subscription_detail_failures = 0
        redemption_detail_failures = 0
        current_trade_keys = {
            stable_key("strategy_trades", row)
            for row in self.collections.get("strategy_trades", [])
        }
        detail_trades = []
        for trade in trades:
            order_type = str(
                field_value(trade, "order_type", "orderType", "type") or ""
            ).lower()
            status = str(field_value(trade, "status", "order_status") or "")
            nonterminal = (
                order_type in {"1", "subscription", "subscribe", "buy"}
                and status != "300"
            ) or (
                order_type in {"2", "redemption", "redeem", "sell"} and status != "400"
            )
            if (
                stable_key("strategy_trades", trade) in current_trade_keys
                or nonterminal
            ):
                detail_trades.append(trade)
        for trade in detail_trades:
            order_type = str(
                field_value(trade, "order_type", "orderType", "type") or ""
            ).lower()
            order_id = field_value(trade, "order_id", "orderId", "id")
            serial = field_value(
                trade, "serial_no", "serialNumber", "serial_number", "sn"
            )
            if order_type in {"1", "subscription", "subscribe", "buy"} and (
                order_id or serial
            ):
                params = {"order_id": order_id} if order_id else {"serial_no": serial}
                call = self._safe_call("/strategy/api/v2/order", params)
                if call:
                    rows = rows_from_payload(
                        call.payload, candidates=("items", "records")
                    )
                    confirmed_at = field_value(trade, "confirm_shares_time")
                    if confirmed_at not in (None, ""):
                        for row in rows:
                            row.setdefault("confirm_shares_time", confirmed_at)
                    self._record_page_audit(
                        call, params, rows, terminal_reason="single_response"
                    )
                    self._store_rows(
                        "subscription_details",
                        rows,
                        request_id=call.request_id,
                        endpoint=call.endpoint.path,
                    )
                    subscriptions.extend(rows)
                else:
                    subscription_detail_failures += 1
            elif (
                order_type in {"2", "redemption", "redeem", "sell"}
                and order_id
                and serial
            ):
                call = self._safe_call(
                    "/strategy/api/v2/redeem",
                    {"order_id": order_id, "serial_no": serial},
                )
                if call:
                    rows = rows_from_payload(
                        call.payload, candidates=("items", "records")
                    )
                    self._record_page_audit(
                        call,
                        {"order_id": order_id, "serial_no": serial},
                        rows,
                        terminal_reason="single_response",
                    )
                    self._store_rows(
                        "redemption_details",
                        rows,
                        request_id=call.request_id,
                        endpoint=call.endpoint.path,
                    )
                    redemptions.extend(rows)
                else:
                    redemption_detail_failures += 1
        self._coverage(
            "/strategy/api/v2/order",
            "complete_to_observed_boundary"
            if subscription_detail_failures == 0
            else "partial",
            terminal_reason="all_discovered_subscription_ids",
            details={
                "detail_rows": len(subscriptions),
                "failures": subscription_detail_failures,
            },
        )
        self._coverage(
            "/strategy/api/v2/redeem",
            "complete_to_observed_boundary"
            if redemption_detail_failures == 0
            else "partial",
            terminal_reason="all_discovered_redemption_ids",
            details={
                "detail_rows": len(redemptions),
                "failures": redemption_detail_failures,
            },
        )

        _reservations, reservations_terminal, reservations_complete = self._offset(
            "/strategy/api/v2/product/reservation/records",
            "reservations",
            base_params={
                "created_at_start": self.history_start_ms,
                "end_time": self.run_end_ms,
            },
            candidates=("items", "records", "reservation_records", "list"),
        )
        if not reservations_complete and reservations_terminal in {
            "repeated_page",
            "max_pages",
        }:
            self._strategy_history_fallback(
                "/strategy/api/v2/product/reservation/records",
                "reservations",
                start_param="created_at_start",
                end_param="end_time",
                candidates=("items", "records", "reservation_records", "list"),
            )
        for reservation in self.previous_collections.get("reservations", []):
            status = str(field_value(reservation, "status", "reservation_status") or "")
            if status in {
                "300",
                "400",
                "completed",
                "cancelled",
                "canceled",
                "expired",
            }:
                continue
            reservation_id = field_value(reservation, "reservation_record_id", "id")
            if reservation_id in (None, ""):
                continue
            self._offset(
                "/strategy/api/v2/product/reservation/records",
                "reservations",
                base_params={"reservation_record_id": reservation_id},
                candidates=("items", "records", "reservation_records", "list"),
                set_coverage=False,
            )

        normal_nav: list[dict[str, Any]] = list(
            self.previous_collections.get("nav_daily", [])
        )
        realtime_nav: list[dict[str, Any]] = list(
            self.previous_collections.get("nav_realtime", [])
        )
        nav_complete = True
        for product_id in sorted(product_ids):
            for window in utc_time_windows(
                self.nav_start_ms,
                self.run_end_ms,
                window_days=366,
                overlap_days=1,
            ):
                params = {
                    "product_id": product_id,
                    "day_timestamp_start": window.start_ms,
                    "day_timestamp_end": window.end_ms,
                }
                call = self._safe_call(
                    "/strategy/api/v2/risk/nav_chart_data_v2",
                    params,
                    core=True,
                )
                if call is None:
                    nav_complete = False
                    continue
                value = _data(call.payload)
                if not isinstance(value, Mapping):
                    continue
                normals = value.get("normal_nav") or value.get("normalNav") or []
                realtime = (
                    value.get("real_time_nav")
                    or value.get("realtime_nav")
                    or value.get("realTimeNav")
                    or []
                )
                normal_rows = [
                    self._prepare_nav(product_id, row, "normal")
                    for row in normals
                    if isinstance(row, Mapping)
                ]
                realtime_rows = [
                    self._prepare_nav(product_id, row, "realtime")
                    for row in realtime
                    if isinstance(row, Mapping)
                ]
                self._record_page_audit(
                    call,
                    params,
                    [*normal_rows, *realtime_rows],
                    terminal_reason="window_response",
                )
                self._store_rows(
                    "nav_daily",
                    normal_rows,
                    request_id=call.request_id,
                    endpoint=call.endpoint.path,
                )
                self._store_rows(
                    "nav_realtime",
                    realtime_rows,
                    request_id=call.request_id,
                    endpoint=call.endpoint.path,
                )
                normal_nav.extend(normal_rows)
                realtime_nav.extend(realtime_rows)
        self._coverage(
            "/strategy/api/v2/risk/nav_chart_data_v2",
            "complete_to_observed_boundary" if nav_complete else "partial",
            terminal_reason="366_day_overlapping_windows",
            requested={"start_ms": self.nav_start_ms, "end_ms": self.run_end_ms},
            observed=(
                {
                    "start_ms": min(
                        timestamp
                        for row in [*normal_nav, *realtime_nav]
                        if (timestamp := record_timestamp_ms(row)) is not None
                    ),
                    "end_ms": max(
                        timestamp
                        for row in [*normal_nav, *realtime_nav]
                        if (timestamp := record_timestamp_ms(row)) is not None
                    ),
                }
                if any(
                    record_timestamp_ms(row) is not None
                    for row in [*normal_nav, *realtime_nav]
                )
                else None
            ),
            details={
                "normal_rows": len(normal_nav),
                "realtime_rows": len(realtime_nav),
            },
        )

        btc_performance: list[dict[str, Any]] = []
        btc_all_complete = True
        btc_requested_starts: dict[str, int] = {}
        for product_id in sorted(product_ids):
            product_record = next(
                (
                    row
                    for row in product_records
                    if str(field_value(row, "product_id", "productId", "id") or "")
                    == product_id
                ),
                {},
            )
            product_start_ms: int | None = None
            for start_field in (
                "btc_perf_start_time",
                "btc_performance_start_time",
                "nav_start_time",
                "listing_time",
                "created_at",
            ):
                value = field_value(product_record, start_field)
                if value not in (None, ""):
                    product_start_ms = record_timestamp_ms({"timestamp_ms": value})
                    if product_start_ms is not None:
                        break
            product_start_ms = max(
                self.history_start_ms, product_start_ms or self.history_start_ms
            )
            btc_requested_starts[product_id] = product_start_ms

            for window in utc_time_windows(
                product_start_ms,
                self.run_end_ms,
                window_days=366,
                overlap_days=1,
            ):
                params = {
                    "product_id": product_id,
                    "timestamp_in_ms_start": window.start_ms,
                    "timestamp_in_ms_end": window.end_ms,
                }
                call = self._safe_call(
                    "/strategy/api/v2/risk/btc_performance_history", params
                )
                if call is None:
                    btc_all_complete = False
                    continue
                rows = rows_from_payload(
                    call.payload,
                    candidates=("items", "records", "list", "btc_performance_history"),
                )
                for row in rows:
                    row.setdefault("product_id", product_id)
                self._record_page_audit(
                    call,
                    params,
                    rows,
                    terminal_reason="window_response",
                )
                self._store_rows(
                    "btc_performance",
                    rows,
                    request_id=call.request_id,
                    endpoint="/strategy/api/v2/risk/btc_performance_history",
                )
                btc_performance.extend(rows)
        self._coverage(
            "/strategy/api/v2/risk/btc_performance_history",
            "complete_to_observed_boundary" if btc_all_complete else "partial",
            terminal_reason="366_day_overlapping_windows",
            requested={"start_ms": self.history_start_ms, "end_ms": self.run_end_ms},
            observed=(
                {
                    "start_ms": min(
                        timestamp
                        for row in btc_performance
                        if (timestamp := record_timestamp_ms(row)) is not None
                    ),
                    "end_ms": max(
                        timestamp
                        for row in btc_performance
                        if (timestamp := record_timestamp_ms(row)) is not None
                    ),
                }
                if any(record_timestamp_ms(row) is not None for row in btc_performance)
                else None
            ),
            details={
                "product_count": len(product_ids),
                "raw_rows": len(btc_performance),
                "product_requested_start_ms": btc_requested_starts,
            },
        )

        rate_by_day: dict[tuple[str, str], Any] = {}
        for row in btc_performance:
            timestamp = field_value(
                row,
                "timestamp_in_ms",
                "timestamp_ms",
                "time_stamp",
                "timestamp",
                "time",
            )
            try:
                timestamp_ms = (
                    int(Decimal(str(timestamp))) if timestamp not in (None, "") else 0
                )
                if 0 < timestamp_ms < 100_000_000_000:
                    timestamp_ms *= 1000
                utc_date = (
                    datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                    .date()
                    .isoformat()
                )
            except Exception:
                continue
            rate = field_value(row, "rate", "usd_rate", "exchange_rate")
            product_id = str(field_value(row, "product_id", "productId") or "")
            if product_id and rate not in (None, ""):
                rate_by_day[(product_id, utc_date)] = rate
        for row in normal_nav:
            timestamp = field_value(row, "timestamp_ms", "day_timestamp", "time_stamp")
            try:
                timestamp_ms = int(Decimal(str(timestamp)))
                if 0 < timestamp_ms < 100_000_000_000:
                    timestamp_ms *= 1000
                utc_date = (
                    datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                    .date()
                    .isoformat()
                )
            except Exception:
                continue
            key = (str(field_value(row, "product_id") or ""), utc_date)
            if key in rate_by_day:
                row.setdefault("fx_rate", rate_by_day[key])
                row.setdefault("fx_rate_source", "btc_performance.rate")

        subscriptions = list(
            {
                stable_key("subscription_details", row): row for row in subscriptions
            }.values()
        )
        redemptions = list(
            {stable_key("redemption_details", row): row for row in redemptions}.values()
        )
        result = build_daily_valuation(
            subscriptions,
            redemptions,
            normal_nav,
            holdings,
            run_end_ms=self.run_end_ms,
            product_metadata=product_records,
        )
        for dataset, rows in result.datasets().items():
            self._store_rows(dataset, rows, request_id=None)
        self.quality_issues.extend(result.quality_issues)

    def _currency_code(self, row: Mapping[str, Any]) -> str:
        return str(
            field_value(row, "currency", "coin", "asset", "symbol", "value") or ""
        ).upper()

    def _extract_wallet(self) -> None:
        self.reporter("wallet: balances, bills, deposits and withdrawals")
        self._single(
            "/mapi/v1/wallet/balance",
            "wallet_balances",
            candidates=("items", "balances", "currencies", "list"),
            mapping_key="currency",
            core=True,
        )
        currencies = self._single(
            "/mapi/v1/wallet/currencies",
            "wallet_currencies",
            candidates=("items", "currencies", "list", "records"),
            mapping_key="currency",
            core=True,
        )

        cursor: str | int | None = None
        seen_cursors: set[str] = set()
        bill_count = 0
        bills_complete = False
        bills_terminal = "max_pages"
        for _page in range(100_000):
            cursor_marker = "<START>" if cursor is None else str(cursor)
            if cursor_marker in seen_cursors:
                bills_terminal = "repeated_cursor"
                break
            seen_cursors.add(cursor_marker)
            params: dict[str, Any] = {
                "limit": OFFSET_PAGE_SIZE,
                "start_time_ms": self.history_start_ms,
                "end_time_ms": self.run_end_ms,
            }
            if cursor is not None:
                params["start_id"] = cursor
            call = self._safe_call("/mapi/v1/wallet/bills", params, core=True)
            if call is None:
                bills_terminal = "request_failed"
                break
            rows = rows_from_payload(
                call.payload, candidates=("bills", "items", "records", "list")
            )
            value = _data(call.payload)
            next_id = value.get("next_id") if isinstance(value, Mapping) else None
            bill_page_terminal = "no_next_id" if next_id in (None, "") else "continue"
            self._record_page_audit(
                call, params, rows, terminal_reason=bill_page_terminal
            )
            self._store_rows(
                "wallet_bills",
                rows,
                request_id=call.request_id,
                endpoint=call.endpoint.path,
            )
            bill_count += len(rows)
            if next_id in (None, ""):
                bills_terminal = "no_next_id"
                bills_complete = True
                break
            cursor = next_id
        self._coverage(
            "/mapi/v1/wallet/bills",
            "complete_to_observed_boundary" if bills_complete else "partial",
            terminal_reason=bills_terminal,
            details={"raw_rows": bill_count},
        )

        codes = sorted(
            {code for row in currencies if (code := self._currency_code(row))}
        )
        for path, dataset in (
            ("/mapi/v1/wallet/deposits", "wallet_deposits"),
            ("/mapi/v1/wallet/withdrawals", "wallet_withdrawals"),
        ):
            all_complete = True
            raw_rows = 0
            failed_currencies: list[str] = []
            for index, currency in enumerate(codes, start=1):
                if index == 1 or index % 50 == 0 or index == len(codes):
                    self.reporter(
                        f"wallet history: {dataset} currency {index}/{len(codes)}"
                    )
                rows, _terminal, complete = self._offset(
                    path,
                    dataset,
                    base_params={"currency": currency},
                    candidates=("items", "records", "deposits", "withdrawals", "list"),
                    set_coverage=False,
                )
                all_complete = all_complete and complete
                if not complete:
                    failed_currencies.append(currency)
                raw_rows += len(rows)
            self._coverage(
                path,
                "complete_to_observed_boundary" if all_complete else "partial",
                terminal_reason=(
                    "all_enumerated_currencies"
                    if all_complete
                    else "enumerated_currency_parameter_rejections"
                ),
                details={
                    "currency_count": len(codes),
                    "raw_rows": raw_rows,
                    "failed_currency_count": len(failed_currencies),
                    "failed_currencies": failed_currencies,
                },
            )

    def _extract_rfq(self) -> None:
        self.reporter("rfq: symbols and full order windows")
        self._single("/trader/v2/api/healthy", "rfq_health", core=False)
        issue_count_before_symbols = len(self.quality_issues)
        symbols = self._single(
            "/trader/v2/api/symbols-info",
            "rfq_symbols",
            candidates=("items", "symbols", "records", "list"),
            mapping_key="symbol",
        )
        symbol_names = sorted(
            {
                str(value)
                for row in symbols
                if (
                    value := field_value(
                        row, "symbol", "pair", "instrument_id", "name", "value"
                    )
                )
                not in (None, "")
            }
        )
        symbols_failed = any(
            issue.get("endpoint") == "/trader/v2/api/symbols-info"
            for issue in self.quality_issues[issue_count_before_symbols:]
        )
        discovered: list[dict[str, Any]] = []
        all_complete = not symbols_failed
        for index, symbol in enumerate(symbol_names, start=1):
            if index == 1 or index % 20 == 0 or index == len(symbol_names):
                self.reporter(f"rfq history: symbol {index}/{len(symbol_names)}")
            failed = False

            def fetch(start_ms: int, end_ms: int, cap: int) -> Sequence[dict[str, Any]]:
                nonlocal failed
                call = self._safe_call(
                    "/trader/v2/api/orders",
                    {
                        "symbol": symbol,
                        "start_time": start_ms,
                        "end_time": end_ms,
                        "limit": cap,
                    },
                )
                if call is None:
                    failed = True
                    return []
                rows = rows_from_payload(
                    call.payload, candidates=("items", "orders", "records", "list")
                )
                for row in rows:
                    row.setdefault("symbol", symbol)
                self._record_page_audit(
                    call,
                    {
                        "symbol": symbol,
                        "start_time": start_ms,
                        "end_time": end_ms,
                        "limit": cap,
                    },
                    rows,
                    terminal_reason="capped_window"
                    if len(rows) >= cap
                    else "window_response",
                )
                self._store_rows(
                    "rfq_orders",
                    rows,
                    request_id=call.request_id,
                    endpoint=call.endpoint.path,
                )
                return rows

            result = paginate_rfq_bisection(
                fetch,
                start_ms=self.history_start_ms,
                end_ms=self.run_end_ms,
                cap=RFQ_CAP,
                key=lambda row: stable_key("rfq_orders", row),
            )
            all_complete = all_complete and result.complete and not failed
            discovered.extend(result.items)
        self._coverage(
            "/trader/v2/api/orders",
            "complete_to_observed_boundary" if all_complete else "partial",
            terminal_reason="all_symbols_adaptive_bisection",
            requested={"start_ms": self.history_start_ms, "end_ms": self.run_end_ms},
            observed=(
                {
                    "start_ms": min(
                        timestamp
                        for row in discovered
                        if (timestamp := record_timestamp_ms(row)) is not None
                    ),
                    "end_ms": max(
                        timestamp
                        for row in discovered
                        if (timestamp := record_timestamp_ms(row)) is not None
                    ),
                }
                if any(record_timestamp_ms(row) is not None for row in discovered)
                else None
            ),
            details={
                "symbol_count": len(symbol_names),
                "unique_orders": len(discovered),
            },
        )

        detail_count = 0
        detail_failures = 0
        for row in discovered:
            order_id = field_value(row, "order_id", "orderId", "id")
            client_order_id = field_value(row, "client_order_id", "clientOrderId")
            if not order_id and not client_order_id:
                continue
            if not order_id or not client_order_id:
                self.quality_issues.append(
                    {
                        "source_table": "rfq_orders",
                        "reason": "single_order_lookup_requires_both_documented_identifiers",
                    }
                )
                continue
            params = {"order_id": order_id, "client_order_id": client_order_id}
            call = self._safe_call("/trader/v2/api/order", params)
            if call:
                records = rows_from_payload(
                    call.payload, candidates=("items", "orders", "records")
                )
                self._record_page_audit(
                    call, params, records, terminal_reason="single_response"
                )
                self._store_rows(
                    "rfq_orders",
                    records,
                    request_id=call.request_id,
                    endpoint=call.endpoint.path,
                )
                detail_count += len(records)
            else:
                detail_failures += 1
        self._coverage(
            "/trader/v2/api/order",
            "complete_to_observed_boundary" if detail_failures == 0 else "partial",
            terminal_reason="all_discovered_order_ids",
            details={"detail_rows": detail_count, "failures": detail_failures},
        )

    def _extract_prime(self) -> None:
        self.reporter("prime broker: account enumeration")
        accounts, terminal, complete = self._offset(
            "/credit/agency/v1/accounts",
            "prime_accounts",
            candidates=("items", "accounts", "records", "list"),
            base_params={},
            core=False,
        )
        dependent_paths = (
            "/credit/agency/v1/account/balance",
            "/credit/agency/v1/account/flows",
            "/credit/agency/v1/account/funds",
        )
        if not accounts:
            reason = "no_accounts" if complete else f"accounts_{terminal}"
            status = "not_applicable" if complete else "partial"
            for path in dependent_paths:
                self._coverage(path, status, terminal_reason=reason)
            return

        for row in accounts:
            account_id = field_value(row, "account_id", "accountId", "id")
            if account_id in (None, ""):
                continue
            self._single(
                "/credit/agency/v1/account/balance",
                "prime_balances",
                params={"account_id": account_id},
                candidates=("items", "balances", "records", "list"),
            )
        self._offset(
            "/credit/agency/v1/account/flows",
            "prime_flows",
            candidates=("items", "flows", "records", "list"),
        )
        self._offset(
            "/credit/agency/v1/account/funds",
            "prime_funds",
            candidates=("items", "funds", "records", "list"),
        )

    def _write_manifests(self) -> None:
        request_rows: list[dict[str, Any]] = []
        lines: list[str] = []
        audits = (
            {
                str(item["row"].get("request_id")): item["row"]
                for item in self.storage.iter_canonical_rows(
                    self.run_id, "request_page_audit"
                )
            }
            if "request_page_audit" in self.storage.dataset_names(self.run_id)
            else {}
        )
        for row in self.storage.iter_requests(self.run_id):
            item = dict(row)
            item["path"] = item["endpoint"]
            item["safe_params"] = item.pop("params_json")
            audit = audits.get(str(item.get("request_id")))
            if audit:
                item["requested_bounds"] = audit.get("requested_bounds", "")
                item["observed_start_ms"] = audit.get("observed_start_ms", "")
                item["observed_end_ms"] = audit.get("observed_end_ms", "")
                item["page_terminal_reason"] = audit.get("terminal_reason", "")
            request_rows.append(item)
            lines.append(canonical_json(item))
        atomic_write(
            self.run_dir / "reports" / "request_manifest.jsonl",
            (("\n".join(lines) + "\n") if lines else "").encode("utf-8"),
        )
        self._store_rows("request_manifest", request_rows, request_id=None)

        coverage = self.storage.list_coverage(self.run_id)
        self._store_rows("coverage", coverage, request_id=None)
        atomic_write(
            self.run_dir / "reports" / "coverage.json",
            (canonical_json(coverage) + "\n").encode("utf-8"),
        )

        self._store_rows("endpoint_catalog", catalog_rows(), request_id=None)
        drift_rows: list[dict[str, Any]] = []
        if self.previous_run_dir is not None:
            current_names = set(self.storage.dataset_names(self.run_id))
            prior_names = set(self.previous_collections)
            for dataset in sorted(current_names | prior_names):
                current_columns: set[str] = set()
                for item in self.storage.iter_canonical_rows(self.run_id, dataset):
                    current_columns.update(str(key) for key in item["row"])
                prior_columns = {
                    str(key)
                    for row in self.previous_collections.get(dataset, [])
                    for key in row
                }
                for column in sorted(current_columns - prior_columns):
                    drift_rows.append(
                        {
                            "dataset": dataset,
                            "column": column,
                            "change": "added",
                            "prior_run_id": self.previous_run_dir.name,
                        }
                    )
                for column in sorted(prior_columns - current_columns):
                    drift_rows.append(
                        {
                            "dataset": dataset,
                            "column": column,
                            "change": "removed",
                            "prior_run_id": self.previous_run_dir.name,
                        }
                    )
        if not drift_rows:
            drift_rows = [
                {
                    "status": "no_column_drift_detected"
                    if self.previous_run_dir
                    else "baseline_run",
                    "note": "All observed fields are retained; no fields are dropped during normalization.",
                }
            ]
        self._store_rows("schema_drift", drift_rows, request_id=None)
        if self.quality_issues:
            self._store_rows("quality_checks", self.quality_issues, request_id=None)
        else:
            self._store_rows(
                "quality_checks",
                [
                    {
                        "status": "PASS",
                        "reason": "no_extraction_or_reconciliation_issues",
                    }
                ],
                request_id=None,
            )

        dictionary: list[dict[str, Any]] = []
        for dataset in self.storage.dataset_names(self.run_id):
            columns: set[str] = set()
            for item in self.storage.iter_canonical_rows(self.run_id, dataset):
                columns.update(str(key) for key in item["row"])
            if not columns:
                dictionary.append(
                    {
                        "dataset": dataset,
                        "column": "",
                        "description": DATASET_DESCRIPTIONS.get(dataset, ""),
                    }
                )
            else:
                for column in sorted(columns):
                    dictionary.append(
                        {
                            "dataset": dataset,
                            "column": column,
                            "description": DATASET_DESCRIPTIONS.get(dataset, ""),
                        }
                    )
        self._store_rows("data_dictionary", dictionary, request_id=None)

    def extract(self) -> ExtractionResult:
        try:
            self.storage.assert_integrity()
            self._initialize_coverage()
            self._extract_strategy()
            self._extract_wallet()
            self._extract_rfq()
            self._extract_prime()
            self._write_manifests()
            counts = {
                name: self.storage.dataset_count(self.run_id, name)
                for name in self.storage.dataset_names(self.run_id)
            }
            request_count = sum(1 for _ in self.storage.iter_requests(self.run_id))
            summary = {
                "run_id": self.run_id,
                "mode": self.mode,
                "status": "COMPLETED",
                "run_end_ms": self.run_end_ms,
                "request_count": request_count,
                "dataset_counts": counts,
            }
            atomic_write(
                self.run_dir / "reports" / "extraction_summary.json",
                (canonical_json(summary) + "\n").encode("utf-8"),
            )
            self.storage.finish_run(self.run_id, status="COMPLETED")
            return ExtractionResult(
                run_id=self.run_id,
                run_dir=self.run_dir,
                status="COMPLETED",
                request_count=request_count,
                dataset_counts=counts,
            )
        except Exception as error:
            row = self.storage.get_run(self.run_id)
            if row and row["status"] == "RUNNING":
                failure = {"type": type(error).__name__, "message": str(error)}
                atomic_write(
                    self.run_dir / "reports" / "failure.json",
                    (canonical_json(failure) + "\n").encode("utf-8"),
                )
                self.storage.finish_run(self.run_id, status="FAILED", failure=failure)
            raise


def load_datasets(run_dir: str | Path) -> dict[str, list[dict[str, Any]]]:
    directory = Path(run_dir)
    validate_run_id(directory.name)
    with RunStorage(directory / "state" / DATABASE_NAME) as storage:
        run = storage.get_run(directory.name)
        if run is None:
            raise ExtractionError(f"Unknown run {directory.name}")
        if run["status"] != "COMPLETED":
            raise ExtractionError(
                f"Run {directory.name} is {run['status']}, not COMPLETED"
            )
        return {
            dataset: [
                item["row"]
                for item in storage.iter_canonical_rows(directory.name, dataset)
            ]
            for dataset in storage.dataset_names(directory.name)
        }


def export_run(workspace: str | Path, run_id: str) -> dict[str, Any]:
    validate_run_id(run_id)
    root = (Path(workspace).resolve() / OUTPUT_FOLDER).resolve()
    run_dir = (root / run_id).resolve()
    if run_dir.parent != root:
        raise ExtractionError("Run path escapes the output directory")
    verification_path = run_dir / "reports" / "verification.json"
    if verification_path.is_file():
        try:
            verified = json.loads(verification_path.read_text(encoding="utf-8"))
        except Exception:
            verified = {}
        if verified.get("ok") is True:
            csv_paths = {
                path.stem: path for path in sorted((run_dir / "csv").glob("*.csv"))
            }
            workbook_paths = {
                kind: run_dir / "xlsx" / f"matrixport_{kind}_{run_id}.xlsx"
                for kind in ("strategy", "account", "audit")
            }
            if not csv_paths or not all(
                path.is_file() for path in workbook_paths.values()
            ):
                raise ExtractionError(
                    "Verified outputs are missing; the immutable run is inconsistent"
                )
            manifest_rows: list[dict[str, Any]] = []
            with (run_dir / "reports" / "dataset_manifest.csv").open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                manifest_rows = list(csv.DictReader(handle))
            return {"csv": csv_paths, "xlsx": workbook_paths, "manifest": manifest_rows}

    try:
        result = export_datasets(run_dir, run_id, load_datasets(run_dir))
    except Exception as error:
        atomic_write(
            run_dir / "reports" / "export_status.json",
            (
                canonical_json({"status": "FAILED", "error": type(error).__name__})
                + "\n"
            ).encode("utf-8"),
        )
        raise
    atomic_write(
        run_dir / "reports" / "export_status.json",
        (
            canonical_json(
                {"status": "COMPLETED", "dataset_count": len(result["manifest"])}
            )
            + "\n"
        ).encode("utf-8"),
    )
    return result


def export_catalog(workspace: str | Path) -> tuple[Path, Path]:
    docs = Path(workspace).resolve() / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    csv_path = write_csv(docs / "endpoint_catalog.csv", catalog_rows())
    json_path = docs / "endpoint_catalog.json"
    atomic_write(json_path, (canonical_json(catalog_rows()) + "\n").encode("utf-8"))
    return csv_path, json_path


def live_auth_test(credentials: ApiCredentials | None = None) -> dict[str, Any]:
    transport = ReadOnlyTransport(credentials or load_credentials())
    response = transport.get("/mapi/v1/wallet/balance")
    payload = _json_loads(response.content)
    code = _api_code(payload)
    if code is not None:
        raise ApiResponseError("/mapi/v1/wallet/balance", code)
    return {
        "ok": True,
        "endpoint": "/mapi/v1/wallet/balance",
        "status_code": response.status_code,
        "request_id": response.request_id,
        "row_count": _row_count(payload),
        "credential_target": DEFAULT_CREDENTIAL_TARGET,
    }
