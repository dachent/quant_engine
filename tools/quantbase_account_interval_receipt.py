"""Issue a closed Quantbase account-interval receipt from retained evidence.

The issuer is deliberately a pure evidence reader: all paths, expected hashes,
source identity material, and interval bounds are caller supplied.  It neither
uses the network nor stages a manifest generation.
"""

from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from quant_engine.core.contracts import (
    GenerationManifest,
    ManifestValidationError,
    canonical_json_bytes,
)
from quant_engine.core.strict_json import strict_json_loads


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_CATEGORIES = ("ACCOUNT_NAV", "CAPITAL_ACTIVITY")
_ACTIVITY_COLUMNS = (
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
)
_ACTIVITY_DATE_FIELDS = (
    "time",
    "date",
    "completed_at",
    "filled_at",
    "created_at",
    "submitted_at",
    "updated_at",
    "canceled_at",
)
_PAGINATION_CONTROL_FIELDS = frozenset(
    {
        "cursor",
        "has_more",
        "has_next",
        "next_cursor",
        "next_page",
        "page",
        "page_number",
        "page_size",
        "pagination",
        "prev_cursor",
        "prev_page",
        "previous_cursor",
        "previous_page",
        "total_pages",
    }
)


class ReceiptIssuanceError(ValueError):
    """Retained evidence does not support the requested closed receipt."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ReceiptIssuanceError(f"{field} must be non-empty canonical text")
    return value


def _iso_date(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ReceiptIssuanceError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != text:
        raise ReceiptIssuanceError(f"{field} must be a canonical ISO date")
    return text


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReceiptIssuanceError(f"{field} must be a lower-case SHA-256")
    return value


def _strict_keys(value: Mapping[str, object], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise ReceiptIssuanceError(
            f"{field} keys differ: missing={sorted(expected - set(value))}, "
            f"unexpected={sorted(set(value) - expected)}"
        )


@dataclass(frozen=True)
class ArtifactInput:
    """One caller-selected artifact and the identity it must have."""

    path: Path
    artifact_id: str
    as_of_date: str
    schema_name: str
    schema_version: str
    expected_sha256: str
    provider_manifest_artifact_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise ReceiptIssuanceError("artifact path must be a pathlib.Path")
        _text(self.artifact_id, "artifact_id")
        _iso_date(self.as_of_date, "artifact as_of_date")
        _text(self.schema_name, "artifact schema_name")
        _text(self.schema_version, "artifact schema_version")
        _digest(self.expected_sha256, "artifact expected_sha256")
        if self.provider_manifest_artifact_id is not None:
            _text(
                self.provider_manifest_artifact_id,
                "provider_manifest_artifact_id",
            )

    @classmethod
    def from_mapping(
        cls, value: object, *, base_dir: Path, field: str
    ) -> ArtifactInput:
        if not isinstance(value, Mapping):
            raise ReceiptIssuanceError(f"{field} must be an object")
        expected = {
            "path",
            "artifact_id",
            "as_of_date",
            "schema_name",
            "schema_version",
            "expected_sha256",
            "provider_manifest_artifact_id",
        }
        _strict_keys(value, expected, field)
        raw_path = _text(value["path"], f"{field}.path")
        path = Path(raw_path)
        if not path.is_absolute():
            path = base_dir / path
        manifest_id = value["provider_manifest_artifact_id"]
        if manifest_id is not None and not isinstance(manifest_id, str):
            raise ReceiptIssuanceError(
                f"{field}.provider_manifest_artifact_id must be text or null"
            )
        return cls(
            path=path,
            artifact_id=value["artifact_id"],
            as_of_date=value["as_of_date"],
            schema_name=value["schema_name"],
            schema_version=value["schema_version"],
            expected_sha256=value["expected_sha256"],
            provider_manifest_artifact_id=manifest_id,
        )


@dataclass(frozen=True)
class RequestInput:
    """Exact retained HTTP request metadata used to select one HAR entry."""

    method: str
    url: str
    expected_status: int

    def __post_init__(self) -> None:
        if self.method != "GET":
            raise ReceiptIssuanceError("request method must be GET")
        _text(self.url, "request url")
        if self.expected_status != 200:
            raise ReceiptIssuanceError("request expected_status must be 200")

    @classmethod
    def from_mapping(cls, value: object, *, field: str) -> RequestInput:
        if not isinstance(value, Mapping):
            raise ReceiptIssuanceError(f"{field} must be an object")
        _strict_keys(value, {"method", "url", "expected_status"}, field)
        return cls(
            method=value["method"],
            url=value["url"],
            expected_status=value["expected_status"],
        )


@dataclass(frozen=True)
class QuantbaseReceiptConfig:
    """Complete explicit input selection for one Quantbase receipt."""

    receipt_artifact_id: str
    account_id: str
    source_identity_material: Mapping[str, object]
    requested_start: str
    requested_end: str
    provider: str
    expected_provider_generation: str
    expected_provider_as_of_date: str
    strategy_fund_title: str
    provider_generation_manifest: ArtifactInput
    provider_catalog: ArtifactInput
    account_binding_authority: ArtifactInput
    activity_response: ArtifactInput
    activity_csv: ArtifactInput
    dashboard_response: ArtifactInput
    nav_csv: ArtifactInput
    extractor: ArtifactInput
    har: ArtifactInput
    activity_request: RequestInput
    dashboard_request: RequestInput

    def __post_init__(self) -> None:
        _text(self.receipt_artifact_id, "receipt_artifact_id")
        _text(self.account_id, "account_id")
        if self.provider != "QUANTBASE":
            raise ReceiptIssuanceError("provider must be QUANTBASE")
        _digest(
            self.expected_provider_generation,
            "expected_provider_generation",
        )
        _iso_date(
            self.expected_provider_as_of_date,
            "expected_provider_as_of_date",
        )
        _text(self.strategy_fund_title, "strategy_fund_title")
        start = _iso_date(self.requested_start, "requested_start")
        end = _iso_date(self.requested_end, "requested_end")
        if start > end:
            raise ReceiptIssuanceError("requested_start exceeds requested_end")
        if not isinstance(self.source_identity_material, Mapping):
            raise ReceiptIssuanceError("source_identity_material must be an object")
        if any(not isinstance(key, str) for key in self.source_identity_material):
            raise ReceiptIssuanceError("source_identity_material keys must be strings")
        for key in ("provider_user_reference", "strategy_fund_title"):
            if key not in self.source_identity_material:
                raise ReceiptIssuanceError(f"source_identity_material is missing {key}")
            _text(self.source_identity_material[key], f"source_identity_material.{key}")
        if (
            self.source_identity_material["strategy_fund_title"]
            != self.strategy_fund_title
        ):
            raise ReceiptIssuanceError("source identity strategy_fund_title differs")
        artifacts = self.evidence_inputs()
        if any(not isinstance(item, ArtifactInput) for item in artifacts):
            raise ReceiptIssuanceError("all evidence inputs must be ArtifactInput")
        ids = [item.artifact_id for item in artifacts]
        if len(ids) != len(set(ids)):
            raise ReceiptIssuanceError("evidence artifact_id values must be unique")
        if not isinstance(self.activity_request, RequestInput) or not isinstance(
            self.dashboard_request, RequestInput
        ):
            raise ReceiptIssuanceError("request inputs must be RequestInput")

    def evidence_inputs(self) -> tuple[ArtifactInput, ...]:
        return (
            self.provider_catalog,
            self.account_binding_authority,
            self.activity_response,
            self.activity_csv,
            self.dashboard_response,
            self.nav_csv,
            self.extractor,
            self.har,
        )

    @classmethod
    def from_mapping(cls, value: object, *, base_dir: Path) -> QuantbaseReceiptConfig:
        if not isinstance(value, Mapping):
            raise ReceiptIssuanceError("config must be an object")
        scalar_fields = {
            "receipt_artifact_id",
            "account_id",
            "source_identity_material",
            "requested_start",
            "requested_end",
            "provider",
            "expected_provider_generation",
            "expected_provider_as_of_date",
            "strategy_fund_title",
        }
        artifact_fields = {
            "provider_generation_manifest",
            "provider_catalog",
            "account_binding_authority",
            "activity_response",
            "activity_csv",
            "dashboard_response",
            "nav_csv",
            "extractor",
            "har",
        }
        request_fields = {"activity_request", "dashboard_request"}
        _strict_keys(value, scalar_fields | artifact_fields | request_fields, "config")
        kwargs: dict[str, object] = {field: value[field] for field in scalar_fields}
        for field in artifact_fields:
            kwargs[field] = ArtifactInput.from_mapping(
                value[field], base_dir=base_dir, field=field
            )
        for field in request_fields:
            kwargs[field] = RequestInput.from_mapping(value[field], field=field)
        return cls(**kwargs)


@dataclass(frozen=True)
class _ReadArtifact:
    spec: ArtifactInput
    contents: bytes
    sha256: str

    @property
    def descriptor(self) -> dict[str, object]:
        return {
            "artifact_id": self.spec.artifact_id,
            "as_of_date": self.spec.as_of_date,
            "schema": {
                "name": self.spec.schema_name,
                "version": self.spec.schema_version,
            },
            "sha256": self.sha256,
            "size_bytes": len(self.contents),
        }


def _read(spec: ArtifactInput, label: str) -> _ReadArtifact:
    try:
        contents = spec.path.read_bytes()
    except OSError as exc:
        raise ReceiptIssuanceError(f"cannot read {label}: {spec.path}") from exc
    observed = hashlib.sha256(contents).hexdigest()
    if observed != spec.expected_sha256:
        raise ReceiptIssuanceError(f"{label} SHA-256 differs")
    return _ReadArtifact(spec=spec, contents=contents, sha256=observed)


def _json_object(contents: bytes, label: str) -> dict[str, object]:
    try:
        parsed = strict_json_loads(contents, label=label)
    except ValueError as exc:
        raise ReceiptIssuanceError(f"{label} is not strict JSON") from exc
    if not isinstance(parsed, dict):
        raise ReceiptIssuanceError(f"{label} root must be an object")
    return parsed


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _csv_rows(contents: bytes, label: str) -> list[list[str]]:
    try:
        text = contents.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReceiptIssuanceError(f"{label} must be UTF-8 CSV") from exc
    try:
        return list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ReceiptIssuanceError(f"{label} is invalid CSV") from exc


def _value_dates(value: Mapping[str, object]) -> list[date]:
    dates: list[date] = []
    for field in _ACTIVITY_DATE_FIELDS:
        raw = value.get(field)
        if raw in (None, ""):
            continue
        if not isinstance(raw, str) or len(raw) < 10:
            raise ReceiptIssuanceError(
                f"capital activity {field} is not an ISO timestamp"
            )
        try:
            parsed = date.fromisoformat(raw[:10])
        except ValueError as exc:
            raise ReceiptIssuanceError(
                f"capital activity {field} is not an ISO timestamp"
            ) from exc
        dates.append(parsed)
    return dates


def _validate_activity_bijection(
    payload: Mapping[str, object],
    csv_contents: bytes,
    config: QuantbaseReceiptConfig,
) -> tuple[date, int]:
    if set(payload) != {"data"} or not isinstance(payload["data"], list):
        raise ReceiptIssuanceError("activity response shape differs")
    entries = payload["data"]
    if not entries:
        raise ReceiptIssuanceError("activity response is empty")
    seen: set[str] = {"activity_type"}
    normalized: list[tuple[str, Mapping[str, object]]] = []
    all_dates: list[date] = []
    fund_dates: list[date] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("type"), str):
            raise ReceiptIssuanceError(f"activity row {index} shape differs")
        item = entry.get("item")
        if not isinstance(item, Mapping):
            raise ReceiptIssuanceError(f"activity row {index} item differs")
        if any(not isinstance(key, str) for key in item):
            raise ReceiptIssuanceError(f"activity row {index} has a non-text field")
        seen.update(item)
        row_dates = _value_dates(item)
        all_dates.extend(row_dates)
        if item.get("fund_title") == config.strategy_fund_title:
            fund_dates.extend(row_dates)
        normalized.append((entry["type"], item))
    unsupported = seen - set(_ACTIVITY_COLUMNS)
    if unsupported:
        raise ReceiptIssuanceError(
            f"activity response contains unsupported columns: {sorted(unsupported)}"
        )
    header = [column for column in _ACTIVITY_COLUMNS if column in seen]
    expected_rows = [
        [
            _cell(item.get(column) if column != "activity_type" else activity_type)
            for column in header
        ]
        for activity_type, item in normalized
    ]
    observed = _csv_rows(csv_contents, "activity CSV")
    if not observed or observed[0] != header or observed[1:] != expected_rows:
        raise ReceiptIssuanceError(
            "activity raw response and CSV are not a fieldwise bijection"
        )
    if not all_dates or not fund_dates:
        raise ReceiptIssuanceError("capital activity date coverage is empty")
    requested_start = date.fromisoformat(config.requested_start)
    requested_end = date.fromisoformat(config.requested_end)
    if min(all_dates) > requested_start:
        raise ReceiptIssuanceError("capital activity does not cover requested_start")
    if max(all_dates) < requested_end:
        raise ReceiptIssuanceError("capital activity does not cover requested_end")
    return min(fund_dates), len(entries)


def _validate_nav_bijection(
    payload: Mapping[str, object],
    csv_contents: bytes,
    config: QuantbaseReceiptConfig,
    first_fund_activity: date,
) -> int:
    if set(payload) != {"data"}:
        raise ReceiptIssuanceError("dashboard response shape differs")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ReceiptIssuanceError("dashboard response data differs")
    funds = data.get("funds_data")
    if not isinstance(funds, Mapping):
        raise ReceiptIssuanceError("dashboard funds_data differs")
    fund = funds.get(config.strategy_fund_title)
    if not isinstance(fund, Mapping):
        raise ReceiptIssuanceError("selected strategy fund is absent from dashboard")
    pricehistory = fund.get("pricehistory")
    if not isinstance(pricehistory, list) or not pricehistory:
        raise ReceiptIssuanceError("selected strategy NAV history is empty")
    header = ["time", "value", "cost_basis", "profit_loss"]
    expected_rows: list[list[str]] = []
    nav_dates: list[date] = []
    for index, row in enumerate(pricehistory):
        if not isinstance(row, Mapping):
            raise ReceiptIssuanceError(f"NAV row {index} shape differs")
        expected_rows.append([_cell(row.get(column)) for column in header])
        raw_date = row.get("time")
        if not isinstance(raw_date, str):
            raise ReceiptIssuanceError(f"NAV row {index} date differs")
        try:
            nav_dates.append(datetime.strptime(raw_date, "%m/%d/%Y").date())
        except ValueError as exc:
            raise ReceiptIssuanceError(f"NAV row {index} date differs") from exc
    if nav_dates != sorted(set(nav_dates)):
        raise ReceiptIssuanceError("NAV dates are not unique ascending observations")
    observed = _csv_rows(csv_contents, "NAV CSV")
    if not observed or observed[0] != header or observed[1:] != expected_rows:
        raise ReceiptIssuanceError(
            "dashboard raw response and NAV CSV are not a fieldwise bijection"
        )
    requested_start = date.fromisoformat(config.requested_start)
    requested_end = date.fromisoformat(config.requested_end)
    if nav_dates[-1] < requested_end:
        raise ReceiptIssuanceError("account NAV does not cover requested_end")
    if nav_dates[0] > requested_start and nav_dates[0] != first_fund_activity:
        raise ReceiptIssuanceError(
            "account NAV begins after the first applicable fund activity"
        )
    return len(pricehistory)


def _validate_account_binding(
    payload: Mapping[str, object],
    raw_bytes: bytes,
    config: QuantbaseReceiptConfig,
) -> None:
    if canonical_json_bytes(payload) != raw_bytes:
        raise ReceiptIssuanceError("account binding authority is not canonical")
    _strict_keys(
        payload,
        {"schema_version", "artifact_id", "as_of_date", "bindings"},
        "account binding authority",
    )
    if payload["schema_version"] != 1:
        raise ReceiptIssuanceError("account binding authority schema differs")
    if payload["artifact_id"] != config.account_binding_authority.artifact_id:
        raise ReceiptIssuanceError("account binding authority artifact_id differs")
    if payload["as_of_date"] != config.account_binding_authority.as_of_date:
        raise ReceiptIssuanceError("account binding authority as_of_date differs")
    bindings = payload["bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise ReceiptIssuanceError("account binding authority bindings differ")
    normalized: list[tuple[str, str, str, str]] = []
    for index, binding in enumerate(bindings):
        if not isinstance(binding, Mapping):
            raise ReceiptIssuanceError(f"account binding {index} shape differs")
        _strict_keys(
            binding,
            {
                "provider",
                "provider_user_reference",
                "strategy_fund_title",
                "account_id",
            },
            f"account binding {index}",
        )
        normalized.append(
            (
                _text(binding["provider"], f"account binding {index}.provider"),
                _text(
                    binding["provider_user_reference"],
                    f"account binding {index}.provider_user_reference",
                ),
                _text(
                    binding["strategy_fund_title"],
                    f"account binding {index}.strategy_fund_title",
                ),
                _text(binding["account_id"], f"account binding {index}.account_id"),
            )
        )
    if normalized != sorted(set(normalized)):
        raise ReceiptIssuanceError(
            "account binding authority must be unique and sorted"
        )
    selected_identity = (
        config.provider,
        str(config.source_identity_material["provider_user_reference"]),
        config.strategy_fund_title,
    )
    matches = [row for row in normalized if row[:3] == selected_identity]
    if len(matches) != 1:
        raise ReceiptIssuanceError("selected source account binding is not unique")
    if matches[0][3] != config.account_id:
        raise ReceiptIssuanceError("account binding does not match internal account_id")


def _validate_provider_catalog(
    payload: Mapping[str, object],
    raw_bytes: bytes,
    config: QuantbaseReceiptConfig,
    evidence: Mapping[str, _ReadArtifact],
    *,
    activity_row_count: int,
    nav_row_count: int,
) -> None:
    if canonical_json_bytes(payload) != raw_bytes:
        raise ReceiptIssuanceError("provider catalog is not canonical")
    expected_keys = {
        "api_fallback",
        "as_of_date",
        "catalog_schema_version",
        "coverage",
        "datasets",
        "family_dataset_counts",
        "family_record_counts",
        "legacy_lineage",
        "planning_reconciliation",
        "provider_id",
        "reconciliation",
    }
    _strict_keys(payload, expected_keys, "provider catalog")
    if (
        payload["catalog_schema_version"] != 1
        or payload["provider_id"] != "quantbase"
        or payload["as_of_date"] != config.expected_provider_as_of_date
        or payload["api_fallback"] is not False
    ):
        raise ReceiptIssuanceError("provider catalog identity differs")
    coverage = payload["coverage"]
    if not isinstance(coverage, Mapping):
        raise ReceiptIssuanceError("provider catalog coverage differs")
    if (
        coverage.get("status") != "COMPLETE_WITHIN_DECLARED_BOUNDARY"
        or coverage.get("known_gaps") != []
        or _iso_date(coverage.get("observed_start"), "catalog observed_start")
        > config.requested_start
        or _iso_date(coverage.get("observed_end"), "catalog observed_end")
        < config.requested_end
    ):
        raise ReceiptIssuanceError("provider catalog coverage differs")
    datasets = payload["datasets"]
    if not isinstance(datasets, list):
        raise ReceiptIssuanceError("provider catalog datasets differ")
    selected = (
        ("activity_csv", "account_activity", activity_row_count),
        ("nav_csv", "dashboard_pricehistory", nav_row_count),
    )
    for role, family, row_count in selected:
        artifact = evidence[role]
        manifest_id = artifact.spec.provider_manifest_artifact_id
        if not isinstance(manifest_id, str) or not manifest_id.startswith("source/"):
            raise ReceiptIssuanceError(f"provider catalog {role} manifest path differs")
        relative_path = manifest_id.removeprefix("source/")
        matches = [
            row
            for row in datasets
            if isinstance(row, Mapping) and row.get("relative_path") == relative_path
        ]
        if len(matches) != 1:
            raise ReceiptIssuanceError(
                f"provider catalog {role} dataset identity differs"
            )
        row = matches[0]
        if row.get("family") != family:
            raise ReceiptIssuanceError(f"provider catalog {role} family differs")
        if row.get("sha256") != artifact.sha256 or row.get("bytes") != len(
            artifact.contents
        ):
            raise ReceiptIssuanceError(
                f"provider catalog {role} artifact identity differs"
            )
        if row.get("row_count") != row_count:
            raise ReceiptIssuanceError(f"provider catalog {role} row_count differs")


@dataclass(frozen=True)
class _HarEntity:
    contents: bytes
    payload: dict[str, object]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.contents).hexdigest()


def _decode_har_body(entry: Mapping[str, object], label: str) -> _HarEntity:
    response = entry.get("response")
    if not isinstance(response, Mapping):
        raise ReceiptIssuanceError(f"{label} HAR response differs")
    content = response.get("content")
    if not isinstance(content, Mapping):
        raise ReceiptIssuanceError(f"{label} HAR response content differs")
    mime = content.get("mimeType")
    if not isinstance(mime, str) or "application/json" not in mime.lower():
        raise ReceiptIssuanceError(f"{label} HAR response is not JSON")
    raw_text = content.get("text")
    if not isinstance(raw_text, str) or not raw_text:
        raise ReceiptIssuanceError(f"{label} HAR response body is empty")
    if content.get("encoding") == "base64":
        try:
            body = base64.b64decode(raw_text, validate=True)
        except (ValueError, TypeError) as exc:
            raise ReceiptIssuanceError(
                f"{label} HAR response body is invalid base64"
            ) from exc
    elif content.get("encoding") in (None, ""):
        body = raw_text.encode("utf-8")
    else:
        raise ReceiptIssuanceError(f"{label} HAR response encoding differs")
    return _HarEntity(
        contents=body,
        payload=_json_object(body, f"{label} HAR response body"),
    )


def _require_quantbase_origin(url: object, label: str) -> str:
    if not isinstance(url, str):
        raise ReceiptIssuanceError(f"{label} request URL differs")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "qbapi3.com"
        or parsed.hostname != "qbapi3.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ReceiptIssuanceError(f"{label} request origin differs")
    return parsed.path


def _is_endpoint_family_path(candidate_path: str, base_path: str) -> bool:
    normalized_base = base_path.rstrip("/")
    return candidate_path.rstrip("/") == normalized_base or candidate_path.startswith(
        normalized_base + "/"
    )


def _reject_pagination_controls(value: object, label: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key.casefold() in _PAGINATION_CONTROL_FIELDS:
                raise ReceiptIssuanceError(
                    f"{label} response contains pagination control {key}"
                )
            _reject_pagination_controls(nested, label)
    elif isinstance(value, list):
        for nested in value:
            _reject_pagination_controls(nested, label)


def _selected_har_entry(
    har: Mapping[str, object], request_input: RequestInput, label: str
) -> Mapping[str, object]:
    log = har.get("log")
    if not isinstance(log, Mapping) or log.get("version") != "1.2":
        raise ReceiptIssuanceError("HAR version differs")
    entries = log.get("entries")
    if not isinstance(entries, list):
        raise ReceiptIssuanceError("HAR entries differ")
    expected_path = _require_quantbase_origin(request_input.url, label)
    family = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ReceiptIssuanceError("HAR entry shape differs")
        request = entry.get("request")
        if not isinstance(request, Mapping):
            continue
        candidate_url = request.get("url")
        if isinstance(candidate_url, str) and _is_endpoint_family_path(
            urlsplit(candidate_url).path, expected_path
        ):
            _require_quantbase_origin(candidate_url, label)
            family.append(entry)
    if len(family) != 1:
        raise ReceiptIssuanceError(f"{label} HAR endpoint family count differs")
    selected = family[0]
    request = selected.get("request")
    response = selected.get("response")
    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        raise ReceiptIssuanceError(f"{label} HAR exchange differs")
    selected_url = request.get("url")
    _require_quantbase_origin(selected_url, label)
    if selected_url != request_input.url:
        raise ReceiptIssuanceError(f"{label} HAR request URL differs")
    if request.get("method") != request_input.method:
        raise ReceiptIssuanceError(f"{label} HAR request method differs")
    if response.get("status") != request_input.expected_status:
        raise ReceiptIssuanceError(f"{label} HAR response status differs")
    parsed_url = urlsplit(request_input.url)
    if request.get("queryString") != [] or parsed_url.query or parsed_url.fragment:
        raise ReceiptIssuanceError(f"{label} request is paginated")
    if request.get("postData") not in (None, {}):
        raise ReceiptIssuanceError(f"{label} GET request unexpectedly has a body")
    return selected


def _provider_user_reference(url: str, suffix: Sequence[str], label: str) -> str:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    suffix_list = list(suffix)
    if len(parts) < len(suffix_list) + 2 or parts[-len(suffix_list) :] != suffix_list:
        raise ReceiptIssuanceError(f"{label} Quantbase endpoint path differs")
    if parts[-len(suffix_list) - 2] != "users":
        raise ReceiptIssuanceError(f"{label} Quantbase endpoint path differs")
    return _text(parts[-len(suffix_list) - 1], f"{label} provider user reference")


def _validate_har(
    har_payload: Mapping[str, object],
    activity_payload: Mapping[str, object],
    dashboard_payload: Mapping[str, object],
    config: QuantbaseReceiptConfig,
) -> _HarEntity:
    activity_entry = _selected_har_entry(
        har_payload, config.activity_request, "activity"
    )
    dashboard_entry = _selected_har_entry(
        har_payload, config.dashboard_request, "dashboard"
    )
    activity_entity = _decode_har_body(activity_entry, "activity")
    dashboard_entity = _decode_har_body(dashboard_entry, "dashboard")
    if activity_entity.payload != activity_payload:
        raise ReceiptIssuanceError("activity HAR response differs from extracted JSON")
    if dashboard_entity.payload != dashboard_payload:
        raise ReceiptIssuanceError("dashboard HAR response differs from extracted JSON")
    activity_user = _provider_user_reference(
        config.activity_request.url, ("accounts", "activity"), "activity"
    )
    dashboard_user = _provider_user_reference(
        config.dashboard_request.url, ("dashboard_data",), "dashboard"
    )
    declared_user = config.source_identity_material["provider_user_reference"]
    if activity_user != dashboard_user or activity_user != declared_user:
        raise ReceiptIssuanceError("source provider user reference differs")
    return activity_entity


def _validate_provider_manifest(
    payload: Mapping[str, object],
    raw_bytes: bytes,
    config: QuantbaseReceiptConfig,
    evidence: Mapping[str, _ReadArtifact],
) -> None:
    if canonical_json_bytes(payload) != raw_bytes:
        raise ReceiptIssuanceError("provider generation manifest is not canonical")
    try:
        manifest = GenerationManifest.from_json_bytes(raw_bytes)
    except ManifestValidationError as exc:
        raise ReceiptIssuanceError(str(exc)) from exc
    if payload.get("manifest_schema_version") != 1:
        raise ReceiptIssuanceError("provider generation manifest schema differs")
    if manifest.generation_id != config.expected_provider_generation:
        raise ReceiptIssuanceError("provider generation id differs")
    if manifest.as_of_date.isoformat() != config.expected_provider_as_of_date:
        raise ReceiptIssuanceError("provider generation as_of_date differs")
    if payload.get("verification") != "VERIFIED":
        raise ReceiptIssuanceError("provider generation is not VERIFIED")
    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ReceiptIssuanceError("provider generation coverage differs")
    if (
        coverage.get("status") != "COMPLETE_WITHIN_DECLARED_BOUNDARY"
        or coverage.get("known_gaps") != []
    ):
        raise ReceiptIssuanceError("provider generation coverage is incomplete")
    observed_start = _iso_date(
        coverage.get("observed_start"), "coverage observed_start"
    )
    observed_end = _iso_date(coverage.get("observed_end"), "coverage observed_end")
    if observed_start > config.requested_start:
        raise ReceiptIssuanceError("provider generation does not cover requested_start")
    if observed_end < config.requested_end:
        raise ReceiptIssuanceError("provider generation does not cover requested_end")
    manifest_artifacts = payload.get("artifacts")
    if not isinstance(manifest_artifacts, list):
        raise ReceiptIssuanceError("provider generation artifacts differ")
    for role in ("provider_catalog", "activity_csv", "nav_csv"):
        selected = evidence[role]
        manifest_id = selected.spec.provider_manifest_artifact_id
        if role == "provider_catalog" and selected.spec.artifact_id != manifest_id:
            raise ReceiptIssuanceError("provider catalog artifact identity differs")
        matches = [
            row
            for row in manifest_artifacts
            if isinstance(row, Mapping) and row.get("artifact_id") == manifest_id
        ]
        expected = selected.descriptor
        if len(matches) != 1 or dict(matches[0]) != {
            "artifact_id": manifest_id,
            "as_of_date": expected["as_of_date"],
            "schema": expected["schema"],
            "sha256": expected["sha256"],
            "size_bytes": expected["size_bytes"],
        }:
            raise ReceiptIssuanceError(
                f"provider generation {role} artifact identity differs"
            )


def _source_identity_sha256(
    config: QuantbaseReceiptConfig,
    account_binding_authority: _ReadArtifact,
) -> str:
    payload = {
        "provider": config.provider,
        "account_id": config.account_id,
        "account_binding_authority": account_binding_authority.descriptor,
        "source_identity_material": dict(config.source_identity_material),
    }
    try:
        encoded = canonical_json_bytes(payload)
        strict_json_loads(encoded, label="source identity material")
    except (TypeError, ValueError) as exc:
        raise ReceiptIssuanceError(
            "source_identity_material is not finite JSON data"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _request_response_chain(
    config: QuantbaseReceiptConfig, activity_entity: _HarEntity
) -> str:
    payload = {
        "method": config.activity_request.method,
        "query_parameters": [],
        "request_url": config.activity_request.url,
        "response_sha256": activity_entity.sha256,
        "response_size_bytes": len(activity_entity.contents),
        "status": config.activity_request.expected_status,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def issue_receipt(config: QuantbaseReceiptConfig) -> bytes:
    """Verify every selected evidence leg and return canonical receipt bytes."""

    if not isinstance(config, QuantbaseReceiptConfig):
        raise ReceiptIssuanceError("config must be QuantbaseReceiptConfig")
    for artifact in (
        config.provider_generation_manifest,
        *config.evidence_inputs(),
    ):
        if artifact.as_of_date != config.expected_provider_as_of_date:
            raise ReceiptIssuanceError(
                "artifact as_of_date differs from provider generation"
            )
    provider_manifest = _read(
        config.provider_generation_manifest, "provider generation manifest"
    )
    evidence = {
        "provider_catalog": _read(config.provider_catalog, "provider catalog"),
        "account_binding_authority": _read(
            config.account_binding_authority, "account binding authority"
        ),
        "activity_response": _read(config.activity_response, "activity response"),
        "activity_csv": _read(config.activity_csv, "activity CSV"),
        "dashboard_response": _read(config.dashboard_response, "dashboard response"),
        "nav_csv": _read(config.nav_csv, "NAV CSV"),
        "extractor": _read(config.extractor, "extractor"),
        "har": _read(config.har, "HAR"),
    }
    activity_payload = _json_object(
        evidence["activity_response"].contents, "activity response"
    )
    dashboard_payload = _json_object(
        evidence["dashboard_response"].contents, "dashboard response"
    )
    catalog_payload = _json_object(
        evidence["provider_catalog"].contents, "provider catalog"
    )
    binding_payload = _json_object(
        evidence["account_binding_authority"].contents,
        "account binding authority",
    )
    provider_payload = _json_object(
        provider_manifest.contents, "provider generation manifest"
    )
    har_payload = _json_object(evidence["har"].contents, "HAR")

    _reject_pagination_controls(activity_payload, "activity")
    _reject_pagination_controls(dashboard_payload, "dashboard")
    first_fund_activity, activity_row_count = _validate_activity_bijection(
        activity_payload, evidence["activity_csv"].contents, config
    )
    nav_row_count = _validate_nav_bijection(
        dashboard_payload,
        evidence["nav_csv"].contents,
        config,
        first_fund_activity,
    )
    _validate_account_binding(
        binding_payload, evidence["account_binding_authority"].contents, config
    )
    activity_entity = _validate_har(
        har_payload, activity_payload, dashboard_payload, config
    )
    _validate_provider_catalog(
        catalog_payload,
        evidence["provider_catalog"].contents,
        config,
        evidence,
        activity_row_count=activity_row_count,
        nav_row_count=nav_row_count,
    )
    _validate_provider_manifest(
        provider_payload,
        provider_manifest.contents,
        config,
        evidence,
    )

    code_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    raw_artifacts = sorted(
        (item.descriptor for item in evidence.values()),
        key=lambda item: str(item["artifact_id"]),
    )
    receipt = {
        "schema_version": 2,
        "artifact_id": config.receipt_artifact_id,
        "issuer": {
            "adapter_name": "quantbase-account-interval-receipt",
            "adapter_version": "1",
            "code_sha256": code_sha256,
        },
        "provider": config.provider,
        "provider_generation": config.expected_provider_generation,
        "provider_generation_manifest": provider_manifest.descriptor,
        "raw_evidence_artifacts": raw_artifacts,
        "account_id": config.account_id,
        "source_account_identity_sha256": _source_identity_sha256(
            config, evidence["account_binding_authority"]
        ),
        "requested_start": config.requested_start,
        "requested_end": config.requested_end,
        "covered_start": config.requested_start,
        "covered_end": config.requested_end,
        "required_categories": list(_REQUIRED_CATEGORIES),
        "completed_categories": list(_REQUIRED_CATEGORIES),
        "unavailable_categories": [],
        "pagination": {
            "mode": "SINGLE_UNPAGINATED_RESPONSE",
            "page_count": 1,
            "request_response_chain_sha256": _request_response_chain(
                config, activity_entity
            ),
            "terminal_response_sha256": activity_entity.sha256,
            "terminal": True,
        },
        "completeness": "EXACT_ACCOUNT_INTERVAL_ALL_APPLICABLE_CATEGORIES",
        "truncated": False,
    }
    return canonical_json_bytes(receipt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Strict JSON receipt input spec; relative paths resolve beside it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Read one explicit config and emit only canonical receipt bytes."""

    args = _parser().parse_args(argv)
    try:
        raw_config = args.config.read_bytes()
    except OSError as exc:
        raise ReceiptIssuanceError(f"cannot read config: {args.config}") from exc
    try:
        parsed = strict_json_loads(raw_config, label="Quantbase receipt config")
    except ValueError as exc:
        raise ReceiptIssuanceError("config is not strict JSON") from exc
    config = QuantbaseReceiptConfig.from_mapping(
        parsed, base_dir=args.config.resolve().parent
    )
    sys.stdout.buffer.write(issue_receipt(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
