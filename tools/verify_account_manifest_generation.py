"""Independent account-manifest verification from immutable selected bytes.

The module owns closed legacy and mixed transports, independently reconstructs
retained authority and lineage, recomputes admitted mixed numeric epochs from
selected provider facts, and emits canonical independent verification reports.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import re
from types import MappingProxyType
from typing import Final, NoReturn

from quant_engine.core import InputSnapshot
from quant_engine.core.contracts import (
    GenerationManifest,
    SourceArtifact,
    canonical_json_bytes,
)
from quant_engine.core.strict_json import strict_json_loads


class IndependentVerificationError(ValueError):
    """A verifier input, subject set, or transport violates its contract."""


EXACT_SUBJECT_PATHS: Final[tuple[str, ...]] = (
    "authority/account-assignment-perimeter.json",
    "authority/account-flow-coverage.json",
    "authority/calculation-inputs.json",
    "authority/classified-flows.json",
    "authority/code-identities.json",
    "authority/flow-classification-policy.json",
    "authority/mapped-epoch-admission.json",
    "authority/selected-input-identities.json",
    "outputs/account-strategy-manifest.json",
)
_SUBJECT_PATH_SET: Final[frozenset[str]] = frozenset(EXACT_SUBJECT_PATHS)
_MIXED_AUTHORITY_PATH: Final[str] = (
    "authority/account-interval-retrieval-receipt-authority.json"
)
_MIXED_FIXED_SUBJECT_PATHS: Final[tuple[str, ...]] = (
    *EXACT_SUBJECT_PATHS,
    _MIXED_AUTHORITY_PATH,
)
_REPORT_PATH: Final[str] = "verification/independent-verification.json"
_CROSSWALK_SUBJECT_PATH: Final[str] = "authority/account-assignment-crosswalk.json"
_REQUEST_VERSION: Final[int] = 1
_CROSSWALK_REQUEST_VERSION: Final[int] = 3
_SNAPSHOT_ROLES: Final[tuple[str, ...]] = (
    "provider_ledger",
    "provider_ledger_manifest",
    "baseline",
    "baseline_manifest",
    "flow_classification_policy",
    "epoch_producer_source",
    "generation_builder_source",
    "independent_verifier_source",
)
_MIXED_SNAPSHOT_ROLES: Final[tuple[str, ...]] = (
    "receipt_authority",
    "admission_policy",
    "manifest_schema",
    "membership_census",
)
APPROVED_MANIFEST_SCHEMA_SHA256: Final[str] = (
    "566d6eb791aa14a7c1afc336d8b53d81a785c6797afa3bc47ada8f8b68017d7c"
)
APPROVED_MANIFEST_SCHEMA_SIZE_BYTES: Final[int] = 35719
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INTERNAL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CROSSWALK_DISPOSITIONS: Final[frozenset[str]] = frozenset(
    {
        "INCLUDED_PRODUCT_EPOCH",
        "EXCLUDED_DEPRECATED_HISTORY_ONLY",
        "EXCLUDED_IMMATERIAL",
    }
)
_CANONICAL_CENT = re.compile(r"^-?(0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$")
_CANONICAL_NUMERIC = re.compile(r"^-?(0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_COUNTERPARTY_RULE = "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
_SOURCE_IDENTITY_COUNTERPARTY_RULE = (
    "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT_WITH_SOURCE_IDENTITY_V1"
)
_PERIOD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "return_window_start",
        "return_close_date",
        "window_days",
        "twr_annualised",
        "ytd_twr",
        "ytd_window",
        "calendar_year_twr",
        "calendar_year_windows",
        "periods_per_year",
        "periods_per_year_basis",
        "mwr_window_equivalent",
        "annualisation_basis",
    }
)
_OUTPUT_FORBIDDEN_NUMERIC_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "start",
        "end",
        "opening_capital",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "nav_observations",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observation_trace",
        "opening_capital_date",
        "performance_status",
        "performance_reason",
        "performance_opening_date",
        "performance_opening_value",
        "performance_opening_basis",
        "performance_flags",
        "performance_flow_legs",
        "flow_count",
        *_PERIOD_FIELDS,
    }
)
_MWR_VALUE_BASES: Final[frozenset[str]] = frozenset(
    {"ACCOUNT_TOTAL", "ACCOUNT_VALUE", "STRATEGY_NAV"}
)
_CENSUS_MEMBERSHIP_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "ASSIGNMENT_ONLY",
        "DEPRECATED_HISTORY_ONLY",
        "EXCLUDED_IMMATERIAL",
        "FUNDED_MEMBERSHIP",
    }
)
_MWR_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "mwr_status",
        "mwr_reason",
        "mwr_xirr",
        "mwr_opening_date",
        "mwr_opening_value",
        "mwr_terminal_date",
        "mwr_terminal_value",
        "mwr_currency",
        "mwr_coverage_status",
        "mwr_cashflows",
        "mwr_label",
        "mwr_value_basis",
        "mwr_valuation_timing",
        "mwr_day_count",
        "mwr_unit",
        "mwr_source_binding",
    }
)
_PORTABLE_COMPONENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _fail() -> NoReturn:
    # Keep errors independent of caller labels, paths, and source contents.
    raise IndependentVerificationError(
        "independent verification input is invalid"
    ) from None


def _require_snapshot(value: object) -> InputSnapshot:
    if type(value) is not InputSnapshot:
        _fail()
    return value


def _independently_validate_current_epoch_crosswalk(
    baseline: InputSnapshot,
    crosswalk: InputSnapshot,
    *,
    baseline_generation_id: str | None = None,
) -> list[dict[str, object]]:
    """Independently bind current subjects; never reuse the producer parser."""

    try:
        baseline_payload = _strict_object(baseline.contents)
        payload = _strict_object(crosswalk.contents)
    except Exception:
        _fail()
    if (
        set(payload)
        != {
            "schema_version",
            "artifact_id",
            "as_of_date",
            "calculation_cutoff_date",
            "disposition_counts",
            "selected_baseline",
            "rows",
        }
        or payload.get("schema_version") != 1
        or payload.get("artifact_id") != "account-strategy-current-epoch-crosswalk"
    ):
        _fail()
    binding = payload.get("selected_baseline")
    expected_binding = {
        "generation_id": baseline_generation_id
        if baseline_generation_id is not None
        else (binding or {}).get("generation_id"),
        "artifact_id": "normalized/portfolio-baseline.json",
        "sha256": baseline.sha256,
        "size_bytes": baseline.size_bytes,
        "as_of_date": baseline_payload.get("as_of_date"),
        "schema_name": "portfolio-baseline-catalog",
        "schema_version": str(baseline_payload.get("baseline_schema_version")),
    }
    if not isinstance(binding, dict) or binding != expected_binding:
        _fail()
    if payload.get("as_of_date") != baseline_payload.get("as_of_date"):
        _fail()
    try:
        if date.fromisoformat(
            str(payload["calculation_cutoff_date"])
        ) > date.fromisoformat(str(payload["as_of_date"])):
            _fail()
    except ValueError:
        _fail()
    accounts = baseline_payload.get("accounts")
    assignments = baseline_payload.get("assignments")
    rows = payload.get("rows")
    if (
        not isinstance(accounts, list)
        or not isinstance(assignments, list)
        or not isinstance(rows, list)
    ):
        _fail()
    providers: dict[str, object] = {}
    for account in accounts:
        if not isinstance(account, dict) or not isinstance(
            account.get("account_id"), str
        ):
            _fail()
        providers[account["account_id"]] = account.get("provider")
    mapped: dict[str, dict[str, object]] = {}
    for assignment in assignments:
        if (
            not isinstance(assignment, dict)
            or assignment.get("strategy_mapping_disposition") != "STRATEGY_MAPPED"
        ):
            continue
        assignment_id = assignment.get("assignment_id")
        if not isinstance(assignment_id, str) or assignment_id in mapped:
            _fail()
        mapped[assignment_id] = assignment
    if len(mapped) != 15 or len(rows) != 15:
        _fail()
    seen: set[str] = set()
    composite_keys: set[tuple[object, object, object, object]] = set()
    labels: dict[str, tuple[object, object, object]] = {}
    included: list[dict[str, object]] = []
    included_intervals: dict[str, list[tuple[date, date | None]]] = {}
    actual_counts = {disposition: 0 for disposition in _CROSSWALK_DISPOSITIONS}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "assignment_id",
            "account_id",
            "provider",
            "strategy_id",
            "effective_start",
            "effective_end",
            "account_label",
            "last_four",
            "product_epoch_disposition",
            "owner_reason",
        }:
            _fail()
        assignment_id = row.get("assignment_id")
        if (
            not isinstance(assignment_id, str)
            or assignment_id in seen
            or assignment_id not in mapped
        ):
            _fail()
        baseline_row = mapped[assignment_id]
        if any(
            row.get(key) != baseline_row.get(key)
            for key in (
                "assignment_id",
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        ) or row.get("provider") != providers.get(row.get("account_id")):
            _fail()
        composite_key = tuple(
            row.get(key)
            for key in ("account_id", "strategy_id", "effective_start", "effective_end")
        )
        if composite_key in composite_keys:
            _fail()
        composite_keys.add(composite_key)
        if (
            row.get("product_epoch_disposition") not in _CROSSWALK_DISPOSITIONS
            or not isinstance(row.get("account_label"), str)
            or not row["account_label"]
        ):
            _fail()
        last_four = row.get("last_four")
        if last_four is not None and (
            not isinstance(last_four, str)
            or re.fullmatch(r"[0-9]{4}", last_four) is None
        ):
            _fail()
        account_id = row.get("account_id")
        if not isinstance(account_id, str):
            _fail()
        label = (row["provider"], row["account_label"], last_four)
        if account_id in labels and labels[account_id] != label:
            _fail()
        labels[account_id] = label
        seen.add(assignment_id)
        disposition = row["product_epoch_disposition"]
        actual_counts[disposition] += 1
        try:
            start = _date_value(row["effective_start"])
            end = (
                None
                if row["effective_end"] is None
                else _date_value(row["effective_end"])
            )
        except Exception:
            _fail()
        if end is not None and end < start:
            _fail()
        if disposition == "INCLUDED_PRODUCT_EPOCH":
            included_intervals.setdefault(account_id, []).append((start, end))
            included.append(dict(baseline_row))
    counts = payload.get("disposition_counts")
    if (
        seen != set(mapped)
        or not isinstance(counts, dict)
        or set(counts) != _CROSSWALK_DISPOSITIONS
        or any(type(value) is not int or value < 0 for value in counts.values())
        or counts != actual_counts
    ):
        _fail()
    for intervals in included_intervals.values():
        intervals.sort(key=lambda interval: interval[0])
        for (_, previous_end), (current_start, _) in zip(intervals, intervals[1:]):
            if previous_end is None or current_start <= previous_end:
                _fail()
    if len(included) != 13:
        _fail()
    included.sort(key=lambda row: str(row["assignment_id"]))
    return included


def _independent_crosswalk_display_by_epoch(
    crosswalk: InputSnapshot,
) -> dict[tuple[object, object, object, object], dict[str, object]]:
    """Independently recover owner-approved display fields by epoch identity."""

    payload = _strict_object(crosswalk.contents)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        _fail()
    result: dict[tuple[object, object, object, object], dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail()
        key = tuple(
            row.get(field)
            for field in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        )
        if key in result:
            _fail()
        result[key] = {
            "account_label": row.get("account_label"),
            "last_four": row.get("last_four"),
        }
    return result


def _independent_crosswalk_dispositions_by_epoch(
    crosswalk: InputSnapshot,
) -> dict[tuple[object, object, object, object], object]:
    """Independently recover the crosswalk's product disposition by epoch identity."""

    payload = _strict_object(crosswalk.contents)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        _fail()
    result: dict[tuple[object, object, object, object], object] = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail()
        key = tuple(
            row.get(field)
            for field in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        )
        if key in result:
            _fail()
        result[key] = row.get("product_epoch_disposition")
    return result


def _independent_epoch_lifecycle(
    effective_end: object, closed_on: object, disposition: object
) -> dict[str, object]:
    """Re-derive an epoch's lifecycle from the assignment end, the epoch's own
    closing date and the crosswalk disposition (#592), with no shared code."""

    if closed_on is not None:
        state = "CLOSED"
    elif effective_end is not None:
        state = "RETIRED"
    elif disposition != "INCLUDED_PRODUCT_EPOCH":
        state = "INACTIVE"
    else:
        state = "ACTIVE"
    return {
        "lifecycle_state": state,
        "retired_on": effective_end if state in ("RETIRED", "CLOSED") else None,
        "closed_on": closed_on if state == "CLOSED" else None,
        "parent_assignment_id": None,
        "sleeve_label": None,
    }


def _validate_approved_manifest_schema(snapshot: InputSnapshot) -> None:
    if (
        snapshot.sha256 != APPROVED_MANIFEST_SCHEMA_SHA256
        or snapshot.size_bytes != APPROVED_MANIFEST_SCHEMA_SIZE_BYTES
    ):
        _fail()


def _portable_identifier(value: object) -> str:
    if not isinstance(value, str) or not value or not value.isascii():
        _fail()
    if (
        value.startswith("/")
        or value.startswith("\\")
        or re.match(r"^[A-Za-z]:", value) is not None
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail()
    components = value.split("/")
    if any(
        not _PORTABLE_COMPONENT.fullmatch(component)
        or component.endswith((".", " "))
        or component.casefold().split(".", 1)[0]
        in {
            "aux",
            "con",
            "conin$",
            "conout$",
            "nul",
            "prn",
            *(f"com{index}" for index in range(1, 10)),
            *(f"lpt{index}" for index in range(1, 10)),
        }
        for component in components
    ):
        _fail()
    return value


def _validate_receipt_ids(
    receipts: Mapping[object, object],
) -> dict[str, InputSnapshot]:
    if not isinstance(receipts, Mapping):
        _fail()
    normalized: dict[str, InputSnapshot] = {}
    folded: set[str] = set()
    for raw_id, raw_snapshot in receipts.items():
        receipt_id = _portable_identifier(raw_id)
        snapshot = _require_snapshot(raw_snapshot)
        if receipt_id.casefold() in folded:
            _fail()
        folded.add(receipt_id.casefold())
        normalized[receipt_id] = snapshot
    return dict(sorted(normalized.items()))


def _selected_date(snapshot: InputSnapshot) -> date:
    try:
        parsed = strict_json_loads(snapshot.contents, label="selected source")
    except ValueError:
        _fail()
    if not isinstance(parsed, dict) or not isinstance(parsed.get("as_of_date"), str):
        _fail()
    value = parsed["as_of_date"]
    try:
        result = date.fromisoformat(value)
    except ValueError:
        _fail()
    if result.isoformat() != value:
        _fail()
    return result


@dataclass(frozen=True)
class IndependentVerifierInputs:
    """Exactly the immutable source snapshots accepted by the verifier."""

    provider_ledger: InputSnapshot
    provider_ledger_manifest: InputSnapshot
    baseline: InputSnapshot
    baseline_manifest: InputSnapshot
    flow_classification_policy: InputSnapshot
    retrieval_receipts: Mapping[str, InputSnapshot]
    epoch_producer_source: InputSnapshot
    generation_builder_source: InputSnapshot
    independent_verifier_source: InputSnapshot
    current_epoch_crosswalk: InputSnapshot | None = None
    receipt_authority: InputSnapshot | None = None
    admission_policy: InputSnapshot | None = None
    manifest_schema: InputSnapshot | None = None
    membership_census: InputSnapshot | None = None
    sleeve_facts: tuple[InputSnapshot, ...] = ()

    def __post_init__(self) -> None:
        for snapshot in _snapshot_roles(self):
            _require_snapshot(snapshot)
        _selected_date(self.provider_ledger)
        _selected_date(self.baseline)
        if self.current_epoch_crosswalk is not None:
            _require_snapshot(self.current_epoch_crosswalk)
        normalized = _validate_receipt_ids(self.retrieval_receipts)
        object.__setattr__(self, "retrieval_receipts", MappingProxyType(normalized))
        facts = tuple(_require_snapshot(item) for item in self.sleeve_facts)
        if facts and self.receipt_authority is None:
            _fail()
        object.__setattr__(self, "sleeve_facts", facts)
        optional = (
            self.receipt_authority,
            self.admission_policy,
            self.manifest_schema,
            self.membership_census,
        )
        if any(value is None for value in optional) != all(
            value is None for value in optional
        ):
            _fail()
        if optional[0] is not None:
            if self.current_epoch_crosswalk is None:
                _fail()
            if not 1 <= len(normalized) <= 15 or any(
                receipt_id.casefold() == _REPORT_PATH.casefold()
                for receipt_id in normalized
            ):
                _fail()
            for snapshot in optional:
                _require_snapshot(snapshot)

    @property
    def generation_as_of_date(self) -> date:
        """Return the later date from the two selected catalog snapshots."""

        return max(_selected_date(self.provider_ledger), _selected_date(self.baseline))

    @property
    def verified_data_as_of_date(self) -> date:
        """Return the boundary through which this verification actually applies.

        A current-product manifest is computed through the crosswalk's
        `calculation_cutoff_date`, which may be earlier than either catalog
        snapshot when the required series end on different days.  The report
        must be dated by what it verified, not by the newest input it read.
        """

        if self.current_epoch_crosswalk is None:
            return self.generation_as_of_date
        payload = self.current_epoch_crosswalk.json_object("current epoch crosswalk")
        cutoff = payload.get("calculation_cutoff_date")
        if not isinstance(cutoff, str):
            _fail()
        try:
            return date.fromisoformat(cutoff)
        except ValueError:
            _fail()

    @property
    def _is_mixed(self) -> bool:
        return self.receipt_authority is not None


def _snapshot_roles(selected: IndependentVerifierInputs) -> tuple[InputSnapshot, ...]:
    """Return the fixed selected snapshots in the transport-role order."""
    return (
        selected.provider_ledger,
        selected.provider_ledger_manifest,
        selected.baseline,
        selected.baseline_manifest,
        selected.flow_classification_policy,
        selected.epoch_producer_source,
        selected.generation_builder_source,
        selected.independent_verifier_source,
    )


def _mixed_snapshot_roles(
    selected: IndependentVerifierInputs,
) -> tuple[InputSnapshot, InputSnapshot, InputSnapshot, InputSnapshot]:
    """Return the closed mixed-only snapshots in transport-role order."""

    return (
        _require_snapshot(selected.receipt_authority),
        _require_snapshot(selected.admission_policy),
        _require_snapshot(selected.manifest_schema),
        _require_snapshot(selected.membership_census),
    )


def _normalize_subject_payloads(
    subject_payloads: Mapping[object, object],
) -> dict[str, bytes]:
    if not isinstance(subject_payloads, Mapping):
        _fail()
    try:
        items_view = subject_payloads.items()
        captured_pairs: list[tuple[object, object]] = []
        for item in items_view:
            try:
                raw_path, raw_contents = item
            except Exception:
                _fail()
            captured_pairs.append((raw_path, raw_contents))
    except Exception:
        _fail()
    paths: list[str] = []
    for raw_path, _ in captured_pairs:
        if type(raw_path) is not str or raw_path not in _SUBJECT_PATH_SET:
            _fail()
        paths.append(raw_path)
    if (
        _REPORT_PATH in paths
        or len(paths) != len(set(paths))
        or set(paths) != _SUBJECT_PATH_SET
    ):
        _fail()
    normalized: dict[str, bytes] = {}
    for raw_path, raw_contents in captured_pairs:
        if type(raw_contents) is not bytes:
            _fail()
        try:
            parsed = strict_json_loads(raw_contents, label="subject payload")
        except ValueError:
            _fail()
        if not isinstance(parsed, dict):
            _fail()
        if canonical_json_bytes(parsed) != raw_contents:
            _fail()
        normalized[raw_path] = bytes(raw_contents)
    return normalized


def _mixed_subject_paths(selected: IndependentVerifierInputs) -> tuple[str, ...]:
    if type(selected) is not IndependentVerifierInputs or not selected._is_mixed:
        _fail()
    if not 1 <= len(selected.retrieval_receipts) <= 15:
        _fail()
    if selected.current_epoch_crosswalk is None:
        _fail()
    receipt_ids = sorted(selected.retrieval_receipts, key=str.casefold)
    fixed_folded = {
        *(path.casefold() for path in _MIXED_FIXED_SUBJECT_PATHS),
        _REPORT_PATH.casefold(),
    }
    if any(receipt_id.casefold() in fixed_folded for receipt_id in receipt_ids):
        _fail()
    authority_snapshot = _require_snapshot(selected.receipt_authority)
    authority = _strict_object(authority_snapshot.contents)
    entries = authority.get("receipts")
    if type(entries) is not list or not entries:
        _fail()
    authority_ids = [
        entry.get("artifact_id") if type(entry) is dict else None for entry in entries
    ]
    if authority_ids != receipt_ids:
        _fail()
    paths = (*_MIXED_FIXED_SUBJECT_PATHS, _CROSSWALK_SUBJECT_PATH, *receipt_ids)
    if len({path.casefold() for path in paths}) != len(paths):
        _fail()
    return paths


def _normalize_subject_payloads_for_paths(
    subject_payloads: Mapping[object, object],
    expected_paths: tuple[str, ...],
) -> dict[str, bytes]:
    if not isinstance(subject_payloads, Mapping):
        _fail()
    try:
        captured_pairs = []
        for item in subject_payloads.items():
            try:
                raw_path, raw_contents = item
            except Exception:
                _fail()
            captured_pairs.append((raw_path, raw_contents))
    except Exception:
        _fail()
    expected = frozenset(expected_paths)
    paths: list[str] = []
    for raw_path, _ in captured_pairs:
        if type(raw_path) is not str or raw_path not in expected:
            _fail()
        paths.append(raw_path)
    if len(paths) != len(set(paths)) or set(paths) != expected:
        _fail()
    normalized: dict[str, bytes] = {}
    for raw_path, raw_contents in captured_pairs:
        if type(raw_contents) is not bytes:
            _fail()
        try:
            parsed = strict_json_loads(raw_contents, label="subject payload")
        except ValueError:
            _fail()
        if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != raw_contents:
            _fail()
        normalized[raw_path] = bytes(raw_contents)
    return normalized


def _normalize_selected_layout_and_subjects(
    selected: IndependentVerifierInputs,
    subject_payloads: Mapping[object, object],
) -> tuple[str, dict[str, bytes]]:
    if type(selected) is not IndependentVerifierInputs:
        _fail()
    if not selected._is_mixed:
        return "legacy", _normalize_subject_payloads(subject_payloads)
    paths = _mixed_subject_paths(selected)
    return "mixed", _normalize_subject_payloads_for_paths(subject_payloads, paths)


def _selected_report_subject_entries(
    selected: IndependentVerifierInputs,
    normalized_subjects: Mapping[str, bytes],
) -> list[dict[str, object]]:
    if not selected._is_mixed:
        return _report_subject_entries(normalized_subjects)
    paths = _mixed_subject_paths(selected)
    if type(normalized_subjects) is not dict or set(normalized_subjects) != set(paths):
        _fail()
    return [
        {
            "path": path,
            "sha256": hashlib.sha256(normalized_subjects[path]).hexdigest(),
            "size_bytes": len(normalized_subjects[path]),
        }
        for path in paths
    ]


def _selected_report_subjects_semantic_sha256(
    selected: IndependentVerifierInputs,
    entries: list[dict[str, object]],
) -> str:
    paths = (
        EXACT_SUBJECT_PATHS
        if not selected._is_mixed
        else _mixed_subject_paths(selected)
    )
    if type(entries) is not list or len(entries) != len(paths):
        _fail()
    for path, entry in zip(paths, entries, strict=True):
        if (
            type(entry) is not dict
            or set(entry) != {"path", "sha256", "size_bytes"}
            or entry.get("path") != path
            or type(entry.get("sha256")) is not str
            or _SHA256.fullmatch(entry["sha256"]) is None
            or type(entry.get("size_bytes")) is not int
            or entry["size_bytes"] < 0
        ):
            _fail()
    return hashlib.sha256(canonical_json_bytes({"subjects": entries})).hexdigest()


def canonical_subject_entries(
    subject_payloads: Mapping[object, object],
) -> tuple[dict[str, object], ...]:
    """Return the sorted, closed path/hash/size subject identity entries."""

    normalized = _normalize_subject_payloads(subject_payloads)
    return tuple(_report_subject_entries(normalized))


def subjects_semantic_sha256(subject_payloads: Mapping[object, object]) -> str:
    """Hash the canonical subject-entry object, excluding the report itself."""

    normalized = _normalize_subject_payloads(subject_payloads)
    entries = _report_subject_entries(normalized)
    return _report_subjects_semantic_sha256(entries)


def _report_subject_entries(
    normalized_subjects: Mapping[str, bytes],
) -> list[dict[str, object]]:
    """Derive fresh exact report subject identities from normalized bytes."""

    if type(normalized_subjects) is not dict:
        _fail()
    if set(normalized_subjects) != _SUBJECT_PATH_SET:
        _fail()
    entries: list[dict[str, object]] = []
    for path in EXACT_SUBJECT_PATHS:
        contents = normalized_subjects.get(path)
        if type(contents) is not bytes:
            _fail()
        try:
            parsed = strict_json_loads(contents, label="normalized subject payload")
        except ValueError:
            _fail()
        if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != contents:
            _fail()
        entries.append(
            {
                "path": path,
                "sha256": hashlib.sha256(contents).hexdigest(),
                "size_bytes": len(contents),
            }
        )
    return entries


def _report_subjects_semantic_sha256(entries: list[dict[str, object]]) -> str:
    """Hash one exact, sorted report subject-entry sequence."""

    if type(entries) is not list or len(entries) != len(EXACT_SUBJECT_PATHS):
        _fail()
    for path, entry in zip(EXACT_SUBJECT_PATHS, entries, strict=True):
        if type(entry) is not dict or set(entry) != {"path", "sha256", "size_bytes"}:
            _fail()
        entry_path = entry.get("path")
        entry_sha256 = entry.get("sha256")
        entry_size = entry.get("size_bytes")
        if (
            type(entry_path) is not str
            or entry_path != path
            or type(entry_sha256) is not str
            or len(entry_sha256) != 64
            or any(character not in "0123456789abcdef" for character in entry_sha256)
            or type(entry_size) is not int
            or entry_size < 0
        ):
            _fail()
    return hashlib.sha256(canonical_json_bytes({"subjects": entries})).hexdigest()


def _strict_object(contents: bytes) -> dict[str, object]:
    try:
        value = strict_json_loads(contents, label="authority subject")
    except ValueError:
        _fail()
    if not isinstance(value, dict):
        _fail()
    return value


def _parse_canonical_date(value: object) -> date:
    if type(value) is not str:
        _fail()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _fail()
    if parsed.isoformat() != value:
        _fail()
    return parsed


def _date_value(value: object) -> date:
    return _parse_canonical_date(value)


def _independent_anchor_fill_date(
    anchor: Mapping[str, object], event_day: date, recognition_day: date
) -> date | None:
    """The UTC fill date one raw ledger capital anchor admits, or None.

    The ledger schema gives a capital anchor ``economic_fill_timestamp`` (a
    timezone-aware UTC instant) and forbids ``economic_date``.  This restates
    the ledger's rule independently of the manifest producer (no import between
    them): the date is the UTC calendar date of the instant, a naive or
    non-UTC instant is rejected rather than converted, and the date must lie in
    ``[event, recognition]``.  An anchor without a timestamp has no fill date; a
    raw anchor that carries ``economic_date`` is invalid.
    """

    if "economic_date" in anchor:
        _fail()
    if "economic_fill_timestamp" not in anchor:
        return None
    raw = anchor["economic_fill_timestamp"]
    if type(raw) is not str or not raw or raw != raw.strip():
        _fail()
    try:
        instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        _fail()
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        _fail()
    fill_day = instant.date()
    if not event_day <= fill_day <= recognition_day:
        _fail()
    return fill_day


def _reject_provider_fact_after_ledger_date(
    fact_date: date, ledger_as_of_date: date
) -> None:
    if fact_date > ledger_as_of_date:
        _fail()


def _validate_provider_ledger_date_authority(
    selected: IndependentVerifierInputs,
    ledger: Mapping[str, object],
) -> date:
    ledger_as_of_date = _selected_date(selected.provider_ledger)
    generations = ledger.get("provider_generations", [])
    if not isinstance(generations, list):
        _fail()
    for generation in generations:
        if not isinstance(generation, dict):
            _fail()
        # Generation provenance is retained source metadata, not a financial
        # observation. Keep its canonical date validation and exact identity
        # binding, but do not compare it with the financial ledger cutoff.
        _date_value(generation.get("generation_as_of_date"))
    return ledger_as_of_date


def _parse_source_cents(value: object) -> Decimal:
    """Parse C3 source money with its canonical one- or two-cent spelling."""

    if type(value) is not str or _CANONICAL_CENT.fullmatch(value) is None:
        _fail()
    with localcontext(
        Context(
            prec=max(40, len(value) + 4),
            rounding=ROUND_HALF_EVEN,
        )
    ):
        try:
            parsed = Decimal(value)
            if (
                not parsed.is_finite()
                or parsed.quantize(Decimal("0.01")) != parsed
                or (value.startswith("-") and parsed.is_zero())
            ):
                _fail()
        except (InvalidOperation, ValueError):
            _fail()
    return parsed


def _parse_retained_source_decimal(value: object) -> Decimal:
    """Parse canonical retained source money without imposing output-cent scale."""

    if type(value) is not str or _CANONICAL_NUMERIC.fullmatch(value) is None:
        _fail()
    with localcontext(
        Context(
            prec=max(40, len(value) + 4),
            rounding=ROUND_HALF_EVEN,
        )
    ):
        try:
            parsed = Decimal(value)
            if not parsed.is_finite() or (value.startswith("-") and parsed.is_zero()):
                _fail()
        except (InvalidOperation, ValueError):
            _fail()
    return parsed


def _format_decimal(value: Decimal) -> str:
    """Format a finite Decimal in canonical fixed-point spelling."""

    if type(value) is not Decimal or not value.is_finite():
        _fail()
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _parse_numeric_output(value: object) -> Decimal:
    """Parse a proposed numeric output's canonical fixed-point string."""

    if type(value) is not str or _CANONICAL_NUMERIC.fullmatch(value) is None:
        _fail()
    with localcontext(
        Context(
            prec=max(40, len(value) + 4),
            rounding=ROUND_HALF_EVEN,
        )
    ):
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            _fail()
    if not parsed.is_finite() or (value.startswith("-") and parsed.is_zero()):
        _fail()
    if _format_decimal(parsed) != value:
        _fail()
    return parsed


def _independent_sign_changes(values: list[Decimal]) -> int:
    changes = 0
    previous = 0
    for value in values:
        sign = (value > 0) - (value < 0)
        if sign and previous and sign != previous:
            changes += 1
        previous = sign or previous
    return changes


def _independent_root_bound(amounts: list[Decimal]) -> int:
    """Bound the IRRs in (-1, inf): forward partial-sum sign changes bound the
    roots with r > 0, reversed ones the roots with -1 < r < 0, and r = 0 is a
    root exactly when the total is zero."""

    def partial_sums(values: list[Decimal]) -> list[Decimal]:
        running = Decimal(0)
        sums: list[Decimal] = []
        for value in values:
            running += value
            sums.append(running)
        return sums

    with localcontext(Context(prec=100, rounding=ROUND_HALF_EVEN)):
        return (
            _independent_sign_changes(partial_sums(amounts))
            + _independent_sign_changes(partial_sums(amounts[::-1]))
            + (1 if sum(amounts, Decimal(0)) == 0 else 0)
        )


def _xirr(cashflows: list[tuple[date, Decimal]]) -> Decimal | None:
    """Compute a unique-root XIRR using an isolated Decimal context."""

    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)) as context:
        by_date: dict[date, Decimal] = {}
        for cashflow_date, amount in cashflows:
            if (
                type(cashflow_date) is not date
                or type(amount) is not Decimal
                or not amount.is_finite()
            ):
                return None
            by_date[cashflow_date] = by_date.get(cashflow_date, Decimal(0)) + amount
        ordered = [
            (cashflow_date, amount)
            for cashflow_date, amount in sorted(by_date.items())
            if amount
        ]
        if (
            len(ordered) < 2
            or _independent_root_bound([amount for _, amount in ordered]) != 1
            or (ordered[0][1] > 0) == (ordered[-1][1] > 0)
        ):
            return None
        start_date = ordered[0][0]

        def npv(rate: Decimal) -> Decimal:
            total = Decimal(0)
            for cashflow_date, amount in ordered:
                exponent = Decimal((cashflow_date - start_date).days) / Decimal(365)
                total += amount / ((Decimal(1) + rate) ** exponent)
            return total

        tolerance = max(abs(amount) for _, amount in ordered) * Decimal("1e-28")
        zero = Decimal(0)
        fzero = npv(zero)
        if abs(fzero) <= tolerance:
            return zero

        bracket: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
        right, fright = zero, fzero
        for exponent in range(1, context.prec + 1):
            left = Decimal(-1) + (Decimal(10) ** -exponent)
            if left == right:
                break
            fleft = npv(left)
            if abs(fleft) <= tolerance:
                return left
            if fleft * fright < 0:
                bracket = (left, right, fleft, fright)
                break
            right, fright = left, fleft

        if bracket is None:
            left, fleft = zero, fzero
            for exponent in range(1, context.prec + 1):
                right = (Decimal(10) ** exponent) - Decimal(1)
                fright = npv(right)
                if abs(fright) <= tolerance:
                    return right
                if fleft * fright < 0:
                    bracket = (left, right, fleft, fright)
                    break
                left, fleft = right, fright

        if bracket is None:
            return None
        low, high, flow, _fhigh = bracket
        for _ in range(400):
            midpoint = (low + high) / 2
            fmidpoint = npv(midpoint)
            if (fmidpoint < 0) == (flow < 0):
                low, flow = midpoint, fmidpoint
            else:
                high = midpoint
        rate = (low + high) / 2
        if abs(npv(rate)) > tolerance:
            return None
        return Decimal(0) if abs(rate) < Decimal("1e-30") else rate


def _annualize_return_on_net_capital(
    return_on_net_capital: Decimal,
    span_days: int,
) -> Decimal:
    """Independently apply the governed piecewise annualization policy."""

    if span_days < 365:
        return return_on_net_capital * Decimal(365) / Decimal(max(span_days, 1))
    if return_on_net_capital <= Decimal(-1):
        _fail()
    return (Decimal(1) + return_on_net_capital) ** (
        Decimal(365) / Decimal(span_days)
    ) - Decimal(1)


@dataclass(frozen=True)
class _IndependentNavFact:
    as_of_date: date
    currency: str
    value: Decimal


_DATE_ONLY: Final[str] = "DATE_ONLY"
_EXACT_TIMESTAMP: Final[str] = "EXACT_TIMESTAMP"
_FLOW_SIGNS: Final[Mapping[str, int]] = MappingProxyType(
    {"EXTERNAL_IN": 1, "EXTERNAL_OUT": -1, "INTERNAL_IN": 1, "INTERNAL_OUT": -1}
)


@dataclass(frozen=True)
class _IndependentFlowFact:
    event_date: date
    classification: str
    currency: str
    amount: Decimal
    nav_recognition_date: date | None = None
    source_record_id: str | None = None
    timing: str = _DATE_ONLY
    economic_date: date | None = None

    @property
    def recognition(self) -> date:
        return self.nav_recognition_date or self.event_date

    @property
    def signed_amount(self) -> Decimal:
        return self.amount * _FLOW_SIGNS[self.classification]

    def order_key(self) -> tuple[str, str, str, str, str, Decimal]:
        return (
            self.event_date.isoformat(),
            self.recognition.isoformat(),
            self.classification,
            self.timing,
            "" if self.economic_date is None else self.economic_date.isoformat(),
            self.amount,
        )


@dataclass(frozen=True)
class _IndependentCapitalAnchorFact:
    event_date: date
    nav_recognition_date: date
    currency: str
    amount: Decimal
    opening_nav: Decimal | None = None


@dataclass(frozen=True)
class _IndependentEpochFacts:
    account_id: str
    start: date
    end: date
    navs: tuple[_IndependentNavFact, ...]
    flows: tuple[_IndependentFlowFact, ...]
    opening_anchor: _IndependentCapitalAnchorFact | None = None
    dual_date_trace: bool = False
    # The account's last observation before ``start`` (R1); ``None`` value
    # means the observation exists but carries no available valuation.
    pre_start_nav: tuple[date, Decimal | None] | None = None
    # The epoch's last funded day when ``end`` carries the account's later
    # observations (the tail of a liquidated account); ``None`` otherwise.
    funded_end: date | None = None
    # A derived sleeve exits in one sweep whose proceeds differ from the last
    # NAV by that day's own P&L, so its liquidation run needs a net outflow.
    derived_sleeve: bool = False


@dataclass(frozen=True)
class _IndependentPredecessorFact:
    account_id: str
    effective_end: date
    closing_nav_date: date
    closing_nav_currency: str
    closing_nav_value: Decimal
    continuity_to_successor: bool


def _fact_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        _fail()
    return value


def _fact_currency(value: object) -> str:
    currency = _fact_text(value)
    if re.fullmatch(r"[A-Z]{3}", currency) is None:
        _fail()
    return currency


_OPTIONAL_FLOW_FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_record_id", "timing", "economic_date"}
)


def _parse_epoch_facts(
    nav_facts: object,
    flow_facts: object,
    *,
    account_id: str,
    start: date,
    end: date,
    _retained_money: bool = False,
    _dual_date_trace: bool = False,
    opening_anchor: _IndependentCapitalAnchorFact | None = None,
    pre_start_nav: tuple[date, Decimal | None] | None = None,
) -> _IndependentEpochFacts:
    """Parse immutable facts for one standalone epoch under one money scale.

    A flow fact is one source record.  Facts carrying no record identity and
    sharing every date, class and timing are one bucketed movement.
    """

    if type(_retained_money) is not bool or type(_dual_date_trace) is not bool:
        _fail()
    parse_money = (
        _parse_retained_source_decimal if _retained_money else _parse_source_cents
    )
    target_account = _fact_text(account_id)
    if type(start) is not date or type(end) is not date or start > end:
        _fail()
    if type(nav_facts) not in (list, tuple) or type(flow_facts) not in (list, tuple):
        _fail()
    navs: list[_IndependentNavFact] = []
    flow_records: list[_IndependentFlowFact] = []
    expected_currency: str | None = None
    for raw in nav_facts:
        if not isinstance(raw, Mapping):
            _fail()
        if set(raw) != {"account_id", "as_of_date", "currency", "value"}:
            _fail()
        raw_account = _fact_text(raw.get("account_id"))
        if raw_account != target_account:
            _fail()
        raw_date = raw["as_of_date"]
        observation_date = _parse_canonical_date(raw_date)
        currency = _fact_currency(raw["currency"])
        value = parse_money(raw["value"])
        if start <= observation_date <= end:
            if expected_currency is None:
                expected_currency = currency
            elif currency != expected_currency:
                _fail()
            navs.append(_IndependentNavFact(observation_date, currency, value))
    navs.sort(key=lambda item: item.as_of_date)
    if not navs or len({item.as_of_date for item in navs}) != len(navs):
        _fail()
    allowed_classes = {
        "EXTERNAL_IN",
        "EXTERNAL_OUT",
        "INTERNAL_IN",
        "INTERNAL_OUT",
    }
    for raw in flow_facts:
        if not isinstance(raw, Mapping):
            _fail()
        expected_flow_fields = {
            "account_id",
            "event_date",
            "classification",
            "currency",
            "amount",
        }
        if _dual_date_trace:
            expected_flow_fields.add("nav_recognition_date")
        if not expected_flow_fields <= set(raw) or not set(raw) - (
            expected_flow_fields
        ) <= (_OPTIONAL_FLOW_FACT_FIELDS):
            _fail()
        raw_account = _fact_text(raw.get("account_id"))
        raw_date = raw["event_date"]
        event_date = _parse_canonical_date(raw_date)
        recognition_date = (
            _parse_canonical_date(raw["nav_recognition_date"])
            if _dual_date_trace
            else event_date
        )
        if recognition_date < event_date:
            _fail()
        classification = _fact_text(raw["classification"])
        if classification not in allowed_classes:
            _fail()
        currency = _fact_currency(raw["currency"])
        amount = parse_money(raw["amount"])
        if amount <= 0:
            _fail()
        source_record_id = raw.get("source_record_id")
        if source_record_id is not None:
            source_record_id = _fact_text(source_record_id)
        timing = raw.get("timing", _DATE_ONLY)
        if timing not in {_DATE_ONLY, _EXACT_TIMESTAMP}:
            _fail()
        economic_raw = raw.get("economic_date")
        economic_date = (
            None if economic_raw is None else _parse_canonical_date(economic_raw)
        )
        if economic_date is not None and (
            timing != _EXACT_TIMESTAMP
            or not event_date <= economic_date <= recognition_date
        ):
            _fail()
        if raw_account == target_account and start <= event_date <= end:
            if expected_currency is None or currency != expected_currency:
                _fail()
            flow_records.append(
                _IndependentFlowFact(
                    event_date,
                    classification,
                    currency,
                    amount,
                    recognition_date,
                    source_record_id,
                    str(timing),
                    economic_date,
                )
            )
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        aggregate: dict[
            tuple[date, date, str, str, str | None, str, date | None], Decimal
        ] = {}
        for flow in flow_records:
            key = (
                flow.event_date,
                flow.recognition,
                flow.classification,
                flow.currency,
                flow.source_record_id,
                flow.timing,
                flow.economic_date,
            )
            aggregate[key] = aggregate.get(key, Decimal(0)) + flow.amount
    flows = sorted(
        (
            _IndependentFlowFact(
                event_date,
                classification,
                currency,
                amount,
                recognition_date,
                source_record_id,
                timing,
                economic_date,
            )
            for (
                event_date,
                recognition_date,
                classification,
                currency,
                source_record_id,
                timing,
                economic_date,
            ), amount in aggregate.items()
        ),
        key=_IndependentFlowFact.order_key,
    )
    if opening_anchor is not None:
        if (
            type(opening_anchor) is not _IndependentCapitalAnchorFact
            or opening_anchor.currency != expected_currency
            or opening_anchor.amount <= 0
            or not start <= opening_anchor.nav_recognition_date <= end
            or opening_anchor.nav_recognition_date < opening_anchor.event_date
            or (
                opening_anchor.opening_nav is not None
                and opening_anchor.opening_nav <= 0
            )
        ):
            _fail()
    if pre_start_nav is not None:
        if (
            type(pre_start_nav) is not tuple
            or len(pre_start_nav) != 2
            or type(pre_start_nav[0]) is not date
            or pre_start_nav[0] >= start
            or (
                pre_start_nav[1] is not None
                and (
                    type(pre_start_nav[1]) is not Decimal
                    or not pre_start_nav[1].is_finite()
                )
            )
        ):
            _fail()
    return _IndependentEpochFacts(
        target_account,
        start,
        end,
        tuple(navs),
        tuple(flows),
        opening_anchor,
        _dual_date_trace,
        pre_start_nav,
    )


def _independent_performance(
    facts: _IndependentEpochFacts,
    days: tuple[_IndependentNavFact, ...],
    predecessor: _IndependentPredecessorFact | None,
) -> dict[str, object]:
    """Recompute R1-R4 epoch performance without the producer's return engine.

    The opening is the predecessor closing NAV, else the last observation
    before the funded start when positive, else the first positive one.  Each
    flow belongs to the sub-period ending at the first observation on or after
    its NAV recognition: exact-timestamp fills join its beginning capital,
    date-only flows leave its ending value.  A zero-capital sub-period is
    neutral only when nothing was earned; otherwise performance is undefined.
    """

    first = days[0]
    anchor = facts.opening_anchor

    def unavailable(
        reason: str, opening: tuple[date, Decimal] | None, basis: str | None
    ) -> dict[str, object]:
        linked = (
            []
            if opening is None
            else [flow for flow in facts.flows if flow.recognition > opening[0]]
        )
        return {
            "observation_linked_return": None,
            "observed_hwm_index": None,
            "observed_max_drawdown": None,
            "observation_trace": {"links": []},
            "performance_status": "UNAVAILABLE",
            "performance_reason": reason,
            "performance_opening_date": (
                None if opening is None else opening[0].isoformat()
            ),
            "performance_opening_value": None if opening is None else opening[1],
            "performance_opening_basis": basis,
            "performance_flags": [],
            "performance_flow_legs": [_independent_leg_row(flow) for flow in linked],
            "flow_count": None if opening is None else len(linked),
        }

    if predecessor is not None:
        opening = (predecessor.closing_nav_date, predecessor.closing_nav_value)
        basis = "PREDECESSOR_CLOSING_NAV"
        if (
            facts.pre_start_nav is not None
            and facts.pre_start_nav[0] > predecessor.closing_nav_date
        ):
            return unavailable(
                "PREDECESSOR_CLOSING_NAV_IS_NOT_LAST_OBSERVATION", None, None
            )
        observations = [opening, *((nav.as_of_date, nav.value) for nav in days)]
    else:
        pre = facts.pre_start_nav
        if pre is not None and pre[1] is None:
            return unavailable("OPENING_OBSERVATION_UNAVAILABLE", None, None)
        if pre is not None and pre[1] is not None and pre[1] > 0:
            opening = (pre[0], pre[1])
            basis = "LAST_OBSERVATION_BEFORE_START"
            if facts.navs[0].as_of_date != first.as_of_date:
                return unavailable(
                    "NONPOSITIVE_OBSERVATION_AFTER_PERFORMANCE_OPENING", opening, basis
                )
            observations = [opening, *((nav.as_of_date, nav.value) for nav in days)]
        else:
            opening = (first.as_of_date, first.value)
            basis = "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START"
            observations = [(nav.as_of_date, nav.value) for nav in days]
    if anchor is not None:
        if anchor.nav_recognition_date > opening[0]:
            return unavailable(
                "OPENING_ANCHOR_AFTER_PERFORMANCE_OPENING", opening, basis
            )
        if (
            anchor.opening_nav is not None
            and anchor.nav_recognition_date == opening[0]
            and anchor.opening_nav != opening[1]
        ):
            return unavailable("OPENING_ANCHOR_NAV_DIFFERS", opening, basis)
    linked = [flow for flow in facts.flows if flow.recognition > opening[0]]
    if any(flow.recognition > observations[-1][0] for flow in linked):
        return unavailable("FLOW_AFTER_CLOSING_OBSERVATION", opening, basis)
    index = Decimal(1)
    hwm = Decimal(1)
    max_drawdown = Decimal(0)
    hundred = Decimal(100)
    links: list[dict[str, object]] = []
    closed_on: date | None = None
    credits: list[dict[str, object]] = []
    for (prior_day, prior_value), (day, value) in zip(observations, observations[1:]):
        if (
            closed_on is None
            and facts.funded_end is not None
            and day > facts.funded_end
        ):
            closed_on = prior_day
        period = [flow for flow in linked if prior_day < flow.recognition <= day]
        beginning = [flow for flow in period if flow.timing == _EXACT_TIMESTAMP]
        beginning_flow = sum((flow.signed_amount for flow in beginning), Decimal(0))
        ending_flow = sum(
            (flow.signed_amount for flow in period if flow.timing == _DATE_ONLY),
            Decimal(0),
        )
        base = prior_value + beginning_flow if beginning else prior_value
        numerator = value - ending_flow
        if closed_on is None and base == 0 and numerator != 0:
            closed_on = prior_day
        if closed_on is not None:
            # Closed at the last funded day: the rest is dated non-TWR income,
            # and a contribution starts a new epoch.
            if any(flow.signed_amount > 0 for flow in period):
                return unavailable("REFUNDING_AFTER_LIQUIDATION", opening, basis)
            if numerator != base:
                credits.append(
                    {
                        "start_date": prior_day.isoformat(),
                        "end_date": day.isoformat(),
                        "beginning_value": prior_value,
                        "ending_value": value,
                        "net_flow": beginning_flow + ending_flow,
                        "amount": numerator - base,
                        "inferred_timing": any(
                            flow.timing == _DATE_ONLY for flow in period
                        ),
                    }
                )
            continue
        if base < 0:
            return unavailable("NEGATIVE_CAPITAL_BASE", opening, basis)
        if base == 0:
            if numerator != 0:
                return unavailable("ZERO_CAPITAL_RETURN_UNDEFINED", opening, basis)
            factor = Decimal(1)
        else:
            factor = numerator / base
            if factor <= 0:
                return unavailable("NONPOSITIVE_LINK_FACTOR", opening, basis)
            index *= factor
        hwm = max(hwm, index)
        drawdown = Decimal(1) - index / hwm
        max_drawdown = max(max_drawdown, drawdown)
        links.append(
            {
                "start_date": prior_day.isoformat(),
                "end_date": day.isoformat(),
                "base_nav": prior_value,
                "beginning_flow": beginning_flow,
                "ending_nav": value,
                "ending_flow": ending_flow,
                "net_flow": beginning_flow + ending_flow,
                "factor": factor,
                "cumulative_index": index * hundred,
                "running_hwm_index": hwm * hundred,
                "drawdown": drawdown,
            }
        )
    result: dict[str, object] = {
        "observation_linked_return": index - Decimal(1),
        "observed_hwm_index": hwm * hundred,
        "observed_max_drawdown": max_drawdown,
        "observation_trace": {"links": links},
        "performance_status": "AVAILABLE",
        "performance_reason": None,
        "performance_opening_date": opening[0].isoformat(),
        "performance_opening_value": opening[1],
        "performance_opening_basis": basis,
        "performance_flags": (
            ["INFERRED_TIMING"]
            if any(
                flow.timing == _DATE_ONLY
                and (closed_on is None or flow.recognition <= closed_on)
                for flow in linked
            )
            else []
        ),
        "performance_flow_legs": [_independent_leg_row(flow) for flow in linked],
        "flow_count": len(linked),
    }
    if closed_on is not None:
        result.update(
            {
                "performance_closed_on": closed_on.isoformat(),
                "post_liquidation_credits": credits,
                "post_liquidation_income": sum(
                    (credit["amount"] for credit in credits), Decimal(0)
                ),
            }
        )
    return result


def _independent_leg_row(flow: _IndependentFlowFact) -> dict[str, object]:
    return {
        "class": flow.classification,
        "event_date": flow.event_date.isoformat(),
        "nav_recognition_date": flow.recognition.isoformat(),
        "economic_date": (
            None if flow.economic_date is None else flow.economic_date.isoformat()
        ),
        "timing": flow.timing,
        "amount": flow.amount,
    }


def _derive_independent_epoch(
    facts: _IndependentEpochFacts,
    predecessor: _IndependentPredecessorFact | None = None,
) -> dict[str, object]:
    """Derive one funded epoch (capital basis and performance) independently."""

    if type(facts) is not _IndependentEpochFacts:
        _fail()
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        first_funded_index = next(
            (index for index, nav in enumerate(facts.navs) if nav.value > 0),
            None,
        )
        if first_funded_index is None:
            _fail()
        funded_navs = facts.navs[first_funded_index:]
        first = funded_navs[0]
        opening_anchor = facts.opening_anchor
        capital_flows = tuple(
            flow
            for flow in facts.flows
            if predecessor is not None or flow.recognition > first.as_of_date
        )

        def signed_flow(start_exclusive: date, end_inclusive: date) -> Decimal:
            return sum(
                (
                    flow.signed_amount
                    for flow in capital_flows
                    if start_exclusive < flow.recognition <= end_inclusive
                ),
                Decimal(0),
            )

        nav_index = 0
        prior_positive = first
        while nav_index < len(funded_navs):
            nav = funded_navs[nav_index]
            if nav.value < 0:
                _fail()
            if nav.value > 0:
                prior_positive = nav
                nav_index += 1
                continue
            first_zero = nav
            zero_end = nav_index
            while (
                zero_end + 1 < len(funded_navs) and funded_navs[zero_end + 1].value == 0
            ):
                zero_end += 1
            # A zero run that ends the window is a liquidation (sold and
            # transferred out); a negative value after it is still refused.
            if (
                (
                    zero_end + 1 < len(funded_navs)
                    and funded_navs[zero_end + 1].value < 0
                )
                or (
                    signed_flow(prior_positive.as_of_date, first_zero.as_of_date) >= 0
                    if facts.derived_sleeve
                    else prior_positive.value
                    + signed_flow(prior_positive.as_of_date, first_zero.as_of_date)
                    != 0
                )
                or any(
                    first_zero.as_of_date
                    < flow.recognition
                    <= funded_navs[zero_end].as_of_date
                    for flow in capital_flows
                )
            ):
                _fail()
            nav_index = zero_end + 1
        days = funded_navs
        if len(days) < 3:
            _fail()
        closing = days[-1]
        if any(flow.event_date > closing.as_of_date for flow in facts.flows):
            _fail()
        if any(flow.recognition > closing.as_of_date for flow in capital_flows):
            _fail()
        if opening_anchor is not None and (
            predecessor is not None
            or opening_anchor.nav_recognition_date != first.as_of_date
            or opening_anchor.event_date > first.as_of_date
        ):
            _fail()

        by_class: dict[str, Decimal] = {
            flow_class: sum(
                (
                    flow.amount
                    for flow in capital_flows
                    if flow.classification == flow_class
                ),
                Decimal(0),
            )
            for flow_class in (
                "EXTERNAL_IN",
                "EXTERNAL_OUT",
                "INTERNAL_IN",
                "INTERNAL_OUT",
            )
        }
        if predecessor is not None:
            opening = predecessor.closing_nav_value
            opening_date = predecessor.closing_nav_date
            opening_basis = "PREDECESSOR_CLOSING_NAV"
        elif opening_anchor is not None:
            opening = opening_anchor.amount
            opening_date = opening_anchor.event_date
            opening_basis = "PROVIDER_CAPITAL_ANCHOR"
        else:
            opening = first.value
            opening_date = first.as_of_date
            opening_basis = "FIRST_AVAILABLE_NAV"
        deposits = by_class["EXTERNAL_IN"]
        invested = opening + deposits + by_class["INTERNAL_IN"]
        withdrawn = by_class["EXTERNAL_OUT"] + by_class["INTERNAL_OUT"]
        net_capital = invested - withdrawn
        current = closing.value
        gain = current - net_capital

        flow_trace = [
            (
                {
                    "class": flow.classification,
                    "event_date": flow.event_date.isoformat(),
                    "nav_recognition_date": flow.recognition.isoformat(),
                    "amount": flow.amount,
                }
                if facts.dual_date_trace
                else {
                    "class": flow.classification,
                    "date": flow.event_date.isoformat(),
                    "amount": flow.amount,
                }
            )
            for flow in capital_flows
        ]
        cashflows: list[tuple[date, Decimal]] = [
            (opening_date, -opening),
            *((flow.event_date, -flow.signed_amount) for flow in capital_flows),
            (closing.as_of_date, current),
        ]
        xirr_rate = _xirr(cashflows)
        span_days = (closing.as_of_date - opening_date).days
        return_on_net_capital = gain / net_capital if net_capital > 0 else None
        annualized = None
        if return_on_net_capital is not None:
            annualized = _annualize_return_on_net_capital(
                return_on_net_capital,
                span_days,
            )
        performance = _independent_performance(facts, days, predecessor)
        return {
            "start": first.as_of_date.isoformat(),
            "end": closing.as_of_date.isoformat(),
            "opening_capital": opening,
            "opening_capital_date": opening_date.isoformat(),
            "deposits": deposits,
            "internal_in": by_class["INTERNAL_IN"],
            "invested": invested,
            "external_withdrawals": by_class["EXTERNAL_OUT"],
            "internal_out": by_class["INTERNAL_OUT"],
            "withdrawn": withdrawn,
            "net_capital": net_capital,
            "current_value": current,
            "gain": gain,
            "return_on_net_capital": return_on_net_capital,
            "annualized_return_on_net_capital": annualized,
            "xirr_diagnostic": xirr_rate,
            "nav_observations": len(days),
            "opening_basis": opening_basis,
            "capital_flow_trace": flow_trace,
            **performance,
        }


def _derive_first_available_nav_epoch(
    facts: _IndependentEpochFacts,
) -> dict[str, object]:
    """Derive one standalone epoch without producer calls."""

    return _derive_independent_epoch(facts)


def _parse_predecessor_fact(raw: object) -> _IndependentPredecessorFact:
    """Parse the closed predecessor continuity fact without trusting authority."""

    required = {
        "account_id",
        "effective_end",
        "closing_nav_date",
        "closing_nav_currency",
        "closing_nav_value",
        "continuity_to_successor",
    }
    if not isinstance(raw, Mapping) or set(raw) != required:
        _fail()
    account_id = _fact_text(raw["account_id"])
    effective_end = _parse_canonical_date(raw["effective_end"])
    closing_nav_date = _parse_canonical_date(raw["closing_nav_date"])
    closing_nav_currency = _fact_currency(raw["closing_nav_currency"])
    closing_nav_value = _parse_source_cents(raw["closing_nav_value"])
    continuity = raw["continuity_to_successor"]
    if (
        type(continuity) is not bool
        or not continuity
        or effective_end != closing_nav_date
        or closing_nav_value <= 0
    ):
        _fail()
    return _IndependentPredecessorFact(
        account_id,
        effective_end,
        closing_nav_date,
        closing_nav_currency,
        closing_nav_value,
        continuity,
    )


def _validate_predecessor_adjacency(
    predecessor: _IndependentPredecessorFact,
    facts: _IndependentEpochFacts,
) -> None:
    if (
        type(predecessor) is not _IndependentPredecessorFact
        or type(facts) is not _IndependentEpochFacts
        or predecessor.effective_end != predecessor.closing_nav_date
        or predecessor.closing_nav_value <= 0
        or predecessor.continuity_to_successor is not True
        or predecessor.account_id != facts.account_id
        or predecessor.closing_nav_date == date.max
        or facts.start != predecessor.closing_nav_date + timedelta(days=1)
        or any(nav.currency != predecessor.closing_nav_currency for nav in facts.navs)
    ):
        _fail()


def _derive_predecessor_closing_nav_epoch(
    predecessor: _IndependentPredecessorFact,
    facts: _IndependentEpochFacts,
) -> dict[str, object]:
    """Derive one successor epoch using its predecessor closing NAV as basis."""

    if (
        type(predecessor) is not _IndependentPredecessorFact
        or type(facts) is not _IndependentEpochFacts
        or len(facts.navs) < 3
        or not facts.navs
        or facts.navs[-1].as_of_date != facts.end
        or any(nav.value <= 0 for nav in facts.navs)
    ):
        _fail()
    _validate_predecessor_adjacency(predecessor, facts)
    return _derive_independent_epoch(facts, predecessor)


def _recognition_legs_attach_to_observed_intervals(
    facts: _IndependentEpochFacts,
    predecessor: _IndependentPredecessorFact | None = None,
) -> bool:
    """Test recognition linkage without deriving or trusting producer numerics.

    Every capital flow must be recognized by an observation of the epoch.  An
    opening that is not a predecessor closing NAV -- a first available NAV or
    a provider capital anchor -- already contains the cash recognized on or
    before it, so those legs carry no linkage obligation.
    """

    funded = tuple(nav for nav in facts.navs if nav.value > 0)
    if not funded:
        return False
    first = funded[0]
    last = facts.navs[-1]
    flows = (
        facts.flows
        if predecessor is not None
        else tuple(flow for flow in facts.flows if flow.recognition > first.as_of_date)
    )
    return all(flow.recognition <= last.as_of_date for flow in flows)


@dataclass(frozen=True)
class _NumericComparison:
    candidate_count: int
    comparison_count: int
    discrepancy_count: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.candidate_count,
                self.comparison_count,
                self.discrepancy_count,
            )
        ):
            _fail()


_D2A_REPORTABILITY_CONTEXT: Final = object()
_D2B_TEST_LOCAL_CONTEXT: Final = object()
_MIXED_REPORTABILITY_CONTEXT: Final = object()


def _validate_reportable_comparison(
    comparison: _NumericComparison,
    *,
    _context: object,
) -> None:
    """Admit only the exact numeric decision for one private context."""

    if type(comparison) is not _NumericComparison:
        _fail()
    try:
        counts = (
            comparison.candidate_count,
            comparison.comparison_count,
            comparison.discrepancy_count,
        )
    except AttributeError:
        _fail()
    if (
        any(type(value) is not int or value < 0 for value in counts)
        or counts[1] > counts[0]
        or counts[2] > counts[1]
        or counts[2] > 0
    ):
        _fail()
    if _context is _D2A_REPORTABILITY_CONTEXT:
        if counts != (0, 0, 0):
            _fail()
    elif _context is _D2B_TEST_LOCAL_CONTEXT:
        if counts != (1, 1, 0):
            _fail()
    elif _context is _MIXED_REPORTABILITY_CONTEXT:
        if counts[0] != counts[1] or counts[2] != 0:
            _fail()
    else:
        _fail()


_NUMERIC_PROJECTION_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "start",
        "end",
        "opening_capital",
        "opening_capital_date",
        "deposits",
        "internal_in",
        "invested",
        "external_withdrawals",
        "internal_out",
        "withdrawn",
        "net_capital",
        "current_value",
        "gain",
        "return_on_net_capital",
        "annualized_return_on_net_capital",
        "xirr_diagnostic",
        "observation_linked_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "nav_observations",
        "observation_trace",
        "capital_flow_trace",
        "performance_status",
        "performance_reason",
        "performance_opening_date",
        "performance_opening_value",
        "performance_opening_basis",
        "performance_flags",
        "performance_flow_legs",
        "flow_count",
    }
)
# Present only for an epoch closed before its last observation.
_POST_LIQUIDATION_FIELDS: Final[frozenset[str]] = frozenset(
    {"performance_closed_on", "post_liquidation_credits", "post_liquidation_income"}
)
_POST_LIQUIDATION_CREDIT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "start_date",
        "end_date",
        "beginning_value",
        "ending_value",
        "net_flow",
        "amount",
        "inferred_timing",
    }
)
_NUMERIC_MONEY_FIELDS: Final[tuple[str, ...]] = (
    "opening_capital",
    "deposits",
    "internal_in",
    "invested",
    "external_withdrawals",
    "internal_out",
    "withdrawn",
    "net_capital",
    "current_value",
    "gain",
)
_NUMERIC_RATE_FIELDS: Final[tuple[str, ...]] = (
    "return_on_net_capital",
    "annualized_return_on_net_capital",
    "xirr_diagnostic",
    "observation_linked_return",
    "observed_hwm_index",
    "observed_max_drawdown",
)
_NUMERIC_TRACE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "start_date",
        "end_date",
        "base_nav",
        "beginning_flow",
        "ending_nav",
        "ending_flow",
        "net_flow",
        "factor",
        "cumulative_index",
        "running_hwm_index",
        "drawdown",
    }
)
_NUMERIC_TRACE_VALUE_FIELDS: Final[tuple[str, ...]] = (
    "base_nav",
    "beginning_flow",
    "ending_nav",
    "ending_flow",
    "net_flow",
    "factor",
    "cumulative_index",
    "running_hwm_index",
    "drawdown",
)
_NUMERIC_TRACE_MONEY_FIELDS: Final[frozenset[str]] = frozenset(
    {"base_nav", "beginning_flow", "ending_nav", "ending_flow", "net_flow"}
)
_NUMERIC_FLOW_FIELDS: Final[frozenset[str]] = frozenset({"class", "date", "amount"})
_NUMERIC_FLOW_FIELDS_V9: Final[frozenset[str]] = frozenset(
    {"class", "event_date", "nav_recognition_date", "amount"}
)
_NUMERIC_LEG_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "class",
        "event_date",
        "nav_recognition_date",
        "economic_date",
        "timing",
        "amount",
    }
)
_NUMERIC_CLASSES: Final[frozenset[str]] = frozenset(
    {"EXTERNAL_IN", "EXTERNAL_OUT", "INTERNAL_IN", "INTERNAL_OUT"}
)
_PERFORMANCE_OPENING_BASES: Final[frozenset[str]] = frozenset(
    {
        "LAST_OBSERVATION_BEFORE_START",
        "FIRST_POSITIVE_OBSERVATION_ON_OR_AFTER_START",
        "PREDECESSOR_CLOSING_NAV",
    }
)


def _projection_decimal(
    value: object,
    *,
    money: bool,
    expected: bool,
    _retained_money: bool = False,
) -> Decimal:
    if type(_retained_money) is not bool:
        _fail()
    if expected:
        if type(value) is not Decimal or not value.is_finite():
            _fail()
        return value
    if money:
        parse_money = (
            _parse_retained_source_decimal if _retained_money else _parse_source_cents
        )
        parsed = parse_money(value)
    else:
        parsed = _parse_numeric_output(value)
    if _format_decimal(parsed) != value:
        _fail()
    return parsed


def _parse_numeric_projection(
    row: object, *, expected: bool, _retained_money: bool = False
) -> tuple[object, ...]:
    if type(_retained_money) is not bool:
        _fail()
    if not isinstance(row, Mapping) or set(row) not in (
        _NUMERIC_PROJECTION_FIELDS,
        _NUMERIC_PROJECTION_FIELDS | _POST_LIQUIDATION_FIELDS,
    ):
        _fail()

    def decimal_value(value: object, *, money: bool) -> Decimal:
        return _projection_decimal(
            value, money=money, expected=expected, _retained_money=_retained_money
        )

    def optional_date(value: object) -> date | None:
        return None if value is None else _parse_canonical_date(value)

    dates = tuple(
        _parse_canonical_date(row[field])
        for field in ("start", "end", "opening_capital_date")
    )
    money = tuple(
        decimal_value(row[field], money=True) for field in _NUMERIC_MONEY_FIELDS
    )
    rates = tuple(
        None if row[field] is None else decimal_value(row[field], money=False)
        for field in _NUMERIC_RATE_FIELDS
    )
    basis = row["opening_basis"]
    if type(basis) is not str or basis not in {
        "FIRST_AVAILABLE_NAV",
        "PREDECESSOR_CLOSING_NAV",
        "PROVIDER_CAPITAL_ANCHOR",
    }:
        _fail()
    nav_observations = row["nav_observations"]
    if type(nav_observations) is not int or nav_observations <= 0:
        _fail()

    trace = row["observation_trace"]
    if not isinstance(trace, Mapping) or set(trace) != {"links"}:
        _fail()
    links = trace["links"]
    if type(links) is not list:
        _fail()
    parsed_links: list[tuple[object, ...]] = []
    for link in links:
        if not isinstance(link, Mapping) or set(link) != _NUMERIC_TRACE_FIELDS:
            _fail()
        parsed_links.append(
            (
                _parse_canonical_date(link["start_date"]),
                _parse_canonical_date(link["end_date"]),
                *(
                    decimal_value(
                        link[field], money=field in _NUMERIC_TRACE_MONEY_FIELDS
                    )
                    for field in _NUMERIC_TRACE_VALUE_FIELDS
                ),
            )
        )

    flow_trace = row["capital_flow_trace"]
    if type(flow_trace) is not list:
        _fail()
    parsed_flows: list[tuple[object, ...]] = []
    dual_date_trace = (
        bool(flow_trace)
        and isinstance(flow_trace[0], Mapping)
        and ("event_date" in flow_trace[0])
    )
    for flow in flow_trace:
        expected_fields = (
            _NUMERIC_FLOW_FIELDS_V9 if dual_date_trace else _NUMERIC_FLOW_FIELDS
        )
        if not isinstance(flow, Mapping) or set(flow) != expected_fields:
            _fail()
        flow_class = flow["class"]
        if type(flow_class) is not str or flow_class not in _NUMERIC_CLASSES:
            _fail()
        parsed_flows.append(
            (
                flow_class,
                *(
                    (
                        _parse_canonical_date(flow["event_date"]),
                        _parse_canonical_date(flow["nav_recognition_date"]),
                    )
                    if dual_date_trace
                    else (_parse_canonical_date(flow["date"]),)
                ),
                decimal_value(flow["amount"], money=True),
            )
        )

    status = row["performance_status"]
    reason = row["performance_reason"]
    opening_basis = row["performance_opening_basis"]
    if (
        status not in {"AVAILABLE", "UNAVAILABLE"}
        or (reason is not None and (type(reason) is not str or not reason))
        or (
            opening_basis is not None
            and opening_basis not in _PERFORMANCE_OPENING_BASES
        )
    ):
        _fail()
    opening_value = row["performance_opening_value"]
    flags = row["performance_flags"]
    if type(flags) is not list or not all(flag == "INFERRED_TIMING" for flag in flags):
        _fail()
    flow_count = row["flow_count"]
    if flow_count is not None and (type(flow_count) is not int or flow_count < 0):
        _fail()
    raw_legs = row["performance_flow_legs"]
    if type(raw_legs) is not list:
        _fail()
    parsed_legs: list[tuple[object, ...]] = []
    for leg in raw_legs:
        if not isinstance(leg, Mapping) or set(leg) != _NUMERIC_LEG_FIELDS:
            _fail()
        if leg["class"] not in _NUMERIC_CLASSES or leg["timing"] not in {
            _DATE_ONLY,
            _EXACT_TIMESTAMP,
        }:
            _fail()
        parsed_legs.append(
            (
                leg["class"],
                _parse_canonical_date(leg["event_date"]),
                _parse_canonical_date(leg["nav_recognition_date"]),
                optional_date(leg["economic_date"]),
                leg["timing"],
                decimal_value(leg["amount"], money=True),
            )
        )
    # Observation metrics exist exactly when the performance is available.
    observation_metrics = rates[3:]
    if (status == "AVAILABLE") != all(
        value is not None for value in observation_metrics
    ):
        _fail()
    if status == "UNAVAILABLE" and (
        any(value is not None for value in observation_metrics)
        or parsed_links
        or flags
        or reason is None
    ):
        _fail()

    post_liquidation: tuple[object, ...] | None = None
    if _POST_LIQUIDATION_FIELDS <= set(row):
        raw_credits = row["post_liquidation_credits"]
        if type(raw_credits) is not list or status != "AVAILABLE":
            _fail()
        parsed_credits: list[tuple[object, ...]] = []
        for credit in raw_credits:
            if (
                not isinstance(credit, Mapping)
                or set(credit) != _POST_LIQUIDATION_CREDIT_FIELDS
                or type(credit["inferred_timing"]) is not bool
            ):
                _fail()
            parsed_credits.append(
                (
                    _parse_canonical_date(credit["start_date"]),
                    _parse_canonical_date(credit["end_date"]),
                    *(
                        decimal_value(credit[field], money=True)
                        for field in (
                            "beginning_value",
                            "ending_value",
                            "net_flow",
                            "amount",
                        )
                    ),
                    credit["inferred_timing"],
                )
            )
        post_liquidation = (
            _parse_canonical_date(row["performance_closed_on"]),
            tuple(parsed_credits),
            decimal_value(row["post_liquidation_income"], money=True),
        )

    return (
        *dates,
        *money,
        *rates,
        basis,
        nav_observations,
        tuple(parsed_links),
        tuple(parsed_flows),
        status,
        reason,
        optional_date(row["performance_opening_date"]),
        None if opening_value is None else decimal_value(opening_value, money=True),
        opening_basis,
        tuple(flags),
        tuple(parsed_legs),
        flow_count,
        post_liquidation,
    )


def _compare_test_local_numeric_epoch(
    fact_form: object, proposed_row: object
) -> _NumericComparison:
    if type(fact_form) is _IndependentEpochFacts:
        expected_row = _derive_first_available_nav_epoch(fact_form)
    elif (
        type(fact_form) is tuple
        and len(fact_form) == 2
        and type(fact_form[0]) is _IndependentPredecessorFact
        and type(fact_form[1]) is _IndependentEpochFacts
    ):
        expected_row = _derive_predecessor_closing_nav_epoch(fact_form[0], fact_form[1])
    else:
        _fail()
    expected_projection = _parse_numeric_projection(expected_row, expected=True)
    candidate_projection = _parse_numeric_projection(proposed_row, expected=False)
    return _NumericComparison(
        candidate_count=1,
        comparison_count=1,
        discrepancy_count=int(candidate_projection != expected_projection),
    )


def _compare_mixed_retained_numeric_epoch(
    fact_form: object, proposed_row: object
) -> _NumericComparison:
    """Compare one mixed epoch while retaining canonical provider precision."""

    if type(fact_form) is _IndependentEpochFacts:
        expected_row = _derive_first_available_nav_epoch(fact_form)
    elif (
        type(fact_form) is tuple
        and len(fact_form) == 2
        and type(fact_form[0]) is _IndependentPredecessorFact
        and type(fact_form[1]) is _IndependentEpochFacts
    ):
        expected_row = _derive_predecessor_closing_nav_epoch(fact_form[0], fact_form[1])
    else:
        _fail()
    expected_projection = _parse_numeric_projection(
        expected_row, expected=True, _retained_money=True
    )
    candidate_projection = _parse_numeric_projection(
        proposed_row, expected=False, _retained_money=True
    )
    return _NumericComparison(
        candidate_count=1,
        comparison_count=1,
        discrepancy_count=int(candidate_projection != expected_projection),
    )


def _independent_mixed_numeric_row(fact_form: object) -> dict[str, object]:
    if type(fact_form) is _IndependentEpochFacts:
        return _derive_first_available_nav_epoch(fact_form)
    if (
        type(fact_form) is tuple
        and len(fact_form) == 2
        and type(fact_form[0]) is _IndependentPredecessorFact
        and type(fact_form[1]) is _IndependentEpochFacts
    ):
        return _derive_predecessor_closing_nav_epoch(fact_form[0], fact_form[1])
    _fail()


def _mwr_blocked_projection(
    status: str, reason: str, *, coverage: str
) -> dict[str, object]:
    if status not in {"BLOCKED_ADMISSION", "INCOMPLETE_FLOW_COVERAGE"}:
        _fail()
    return {
        "mwr_status": status,
        "mwr_reason": reason,
        "mwr_xirr": None,
        "mwr_opening_date": None,
        "mwr_opening_value": None,
        "mwr_terminal_date": None,
        "mwr_terminal_value": None,
        "mwr_currency": None,
        "mwr_coverage_status": coverage,
        "mwr_cashflows": [],
        "mwr_label": None,
        "mwr_value_basis": None,
        "mwr_valuation_timing": None,
        "mwr_day_count": "ACTUAL_365",
        "mwr_unit": "ANNUALIZED_RETURN",
        "mwr_source_binding": "GOVERNED_SOURCE_IDENTITIES",
    }


def _classify_independent_mwr(
    cashflows: list[tuple[date, Decimal]],
) -> tuple[str, str | None, Decimal | None]:
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        by_date: dict[date, Decimal] = {}
        for when, amount in cashflows:
            by_date[when] = by_date.get(when, Decimal(0)) + amount
        ordered = [(when, amount) for when, amount in sorted(by_date.items()) if amount]
        if not ordered:
            return "INDETERMINATE", "ALL_CASHFLOWS_ZERO", None
        signs = [amount > 0 for _, amount in ordered]
        changes = sum(left != right for left, right in zip(signs, signs[1:]))
        if changes == 0:
            return "NO_ROOT", "NO_CHRONOLOGICAL_SIGN_CHANGE", None
        bound = _independent_root_bound([amount for _, amount in ordered])
        if bound == 0 or (bound == 1 and signs[0] == signs[-1]):
            return "NO_ROOT", "NO_PARTIAL_SUM_SIGN_CHANGE", None
        if bound > 1:
            return "UNIQUENESS_NOT_PROVED", "MULTIPLE_CHRONOLOGICAL_SIGN_CHANGES", None
        rate = _xirr(ordered)
        if rate is None:
            return "NUMERICAL_SEARCH_UNRESOLVED", "UNIQUE_ROOT_SEARCH_UNRESOLVED", None
        return "AVAILABLE", None, rate


def _derive_independent_mwr_projection(
    assignment: Mapping[str, object],
    fact_form: object,
    bridge: Mapping[str, object],
) -> dict[str, object]:
    """Rebuild the source-bound MWR vector without calling the producer.

    Investor cash flows start at the performance opening valuation; a
    date-only leg is dated at its NAV recognition, an exact-timestamp fill at
    its economic fill date, and without that date the MWR fails closed (R4).
    """

    numeric = _independent_mixed_numeric_row(fact_form)
    ledger = bridge.get("ledger")
    events = bridge.get("events")
    anchors = bridge.get("capital_anchors")
    if (
        not isinstance(ledger, Mapping)
        or not isinstance(events, list)
        or not isinstance(anchors, list)
    ):
        _fail()
    account_id = _fact_text(assignment.get("account_id"))
    strategy_id = _fact_text(assignment.get("strategy_id"))
    raw_opening_date = numeric.get("performance_opening_date")
    if raw_opening_date is None:
        return _mwr_blocked_projection(
            "BLOCKED_ADMISSION", "PERFORMANCE_OPENING_UNAVAILABLE", coverage="BLOCKED"
        )
    opening_date = _parse_canonical_date(raw_opening_date)
    terminal_date = _parse_canonical_date(numeric.get("end"))
    opening_value = numeric.get("performance_opening_value")
    terminal_value = numeric.get("current_value")
    if type(opening_value) is not Decimal or type(terminal_value) is not Decimal:
        _fail()

    nav_sources: dict[tuple[str, date], list[tuple[str, str, str]]] = {}
    navs = ledger.get("nav_observations")
    if not isinstance(navs, list):
        _fail()
    for nav in navs:
        if not isinstance(nav, Mapping):
            _fail()
        nav_account = _fact_text(nav.get("account_id"))
        nav_date = _parse_canonical_date(nav.get("as_of_date"))
        record_id = _fact_text(nav.get("record_id"))
        basis = _fact_text(nav.get("value_basis"))
        if basis not in _MWR_VALUE_BASES:
            _fail()
        money = nav.get("nav")
        if not isinstance(money, Mapping):
            _fail()
        currency = _fact_currency(money.get("currency"))
        nav_sources.setdefault((nav_account, nav_date), []).append(
            (record_id, basis, currency)
        )

    opening_anchor_ids: set[str] = set()
    allocation_economics: dict[
        tuple[str, str, date, date, Decimal], dict[date | None, set[str]]
    ] = {}
    for anchor in anchors:
        if not isinstance(anchor, Mapping):
            _fail()
        anchor_account = _fact_text(anchor.get("account_id"))
        anchor_strategy = _fact_text(anchor.get("strategy_id"))
        anchor_type = _fact_text(anchor.get("capital_type"))
        event_date = _parse_canonical_date(anchor.get("event_date"))
        recognition_date = _parse_canonical_date(anchor.get("nav_recognition_date"))
        source_id = _fact_text(anchor.get("source_record_id"))
        # Every anchor's own fill field is validated here (an opening anchor's
        # included, though it dates no MWR leg); the ledger writes
        # ``economic_fill_timestamp`` and never ``economic_date``.
        economic_date = _independent_anchor_fill_date(
            anchor, event_date, recognition_date
        )
        if anchor_type == "STRATEGY_CAPITAL_ALLOCATION":
            money = anchor.get("amount")
            if not isinstance(money, Mapping):
                _fail()
            # The raw anchor's fill date is part of the allocation identity:
            # an exact-timestamp fill binds its MWR cash date to this source
            # value, never to a published flow leg's own economic date.
            allocation_economics.setdefault(
                (
                    anchor_account,
                    anchor_strategy,
                    event_date,
                    recognition_date,
                    _parse_retained_source_decimal(money.get("value")),
                ),
                {},
            ).setdefault(economic_date, set()).add(source_id)
        elif anchor_type == "OPENING_CAPITAL_ANCHOR":
            if (
                anchor_account == account_id
                and anchor_strategy == strategy_id
                and recognition_date == opening_date
            ):
                opening_anchor_ids.add(source_id)
        else:
            _fail()

    opening_navs = nav_sources.get((account_id, opening_date), [])
    terminal_navs = nav_sources.get((account_id, terminal_date), [])
    if not opening_navs or not terminal_navs:
        return _mwr_blocked_projection(
            "BLOCKED_ADMISSION", "NAV_SOURCE_IDENTITY_UNAVAILABLE", coverage="COMPLETE"
        )
    perimeter = {*opening_navs, *terminal_navs}
    bases = {item[1] for item in perimeter}
    currencies = {item[2] for item in perimeter}
    if len(bases) != 1 or len(currencies) != 1:
        return _mwr_blocked_projection(
            "BLOCKED_ADMISSION",
            "NAV_PERIMETER_OR_CURRENCY_MISMATCH",
            coverage="COMPLETE",
        )
    basis = next(iter(bases))
    currency = next(iter(currencies))

    event_ids: dict[tuple[str, str, date, date, Decimal], set[str]] = {}
    for event in events:
        if not isinstance(event, Mapping):
            _fail()
        event_account = _fact_text(event.get("account_id"))
        amount = _parse_retained_source_decimal(event.get("amount"))
        if amount == 0:
            _fail()
        disposition = event.get("disposition")
        if disposition not in {"EXTERNAL", "INTERNAL"}:
            _fail()
        event_ids.setdefault(
            (
                event_account,
                f"{disposition}_{'IN' if amount > 0 else 'OUT'}",
                _parse_canonical_date(event.get("event_date")),
                _parse_canonical_date(event.get("nav_recognition_date")),
                abs(amount),
            ),
            set(),
        ).add(_fact_text(event.get("source_record_id")))

    legs = numeric.get("performance_flow_legs")
    if not isinstance(legs, list):
        _fail()
    complete = True
    dated: list[tuple[date, Decimal, set[str]]] = []
    missing_fill_date = False
    for leg in legs:
        if not isinstance(leg, Mapping):
            _fail()
        flow_class = _fact_text(leg.get("class"))
        event_date = _parse_canonical_date(leg.get("event_date"))
        recognition_date = _parse_canonical_date(leg.get("nav_recognition_date"))
        amount = leg.get("amount")
        if type(amount) is not Decimal:
            _fail()
        economic = leg.get("economic_date")
        if leg.get("timing") == _EXACT_TIMESTAMP and flow_class == "INTERNAL_IN":
            economics = allocation_economics.get(
                (account_id, strategy_id, event_date, recognition_date, amount)
            )
            if economics is None:
                source_ids: set[str] = set()
            elif economic is None:
                source_ids = economics.get(None, set())
            else:
                leg_economic = _parse_canonical_date(economic)
                # The published leg's economic date must bind to the raw
                # anchor's economic date; the MWR cash date is that matched
                # source value, and a differing published date fails closed.
                source_ids = economics.get(leg_economic, set())
                if not source_ids:
                    _fail()
        else:
            source_ids = event_ids.get(
                (account_id, flow_class, event_date, recognition_date, amount), set()
            )
        complete = complete and bool(source_ids)
        if leg.get("timing") == _EXACT_TIMESTAMP:
            if economic is None:
                missing_fill_date = True
                continue
            cash_date = _parse_canonical_date(economic)
        else:
            cash_date = recognition_date
        with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
            investor_amount = (
                -amount if flow_class in {"EXTERNAL_IN", "INTERNAL_IN"} else amount
            )
        dated.append((cash_date, investor_amount, set(source_ids)))
    if not complete:
        return _mwr_blocked_projection(
            "BLOCKED_ADMISSION", "FLOW_SOURCE_IDENTITY_UNAVAILABLE", coverage="COMPLETE"
        )
    if missing_fill_date:
        return _mwr_blocked_projection(
            "BLOCKED_ADMISSION",
            "EXACT_TIMESTAMP_FILL_DATE_UNAVAILABLE",
            coverage="BLOCKED",
        )
    if any(cash_date < opening_date for cash_date, _, _ in dated):
        _fail()

    grouped: dict[date, tuple[Decimal, set[str]]] = {}

    def add(when: date, amount: Decimal, source_ids: set[str]) -> None:
        prior_amount, prior_ids = grouped.get(when, (Decimal(0), set()))
        with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
            grouped[when] = (prior_amount + amount, prior_ids | source_ids)

    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        opening_leg = -opening_value
    add(
        opening_date,
        opening_leg,
        {item[0] for item in opening_navs} | opening_anchor_ids,
    )
    for cash_date, investor_amount, source_ids in dated:
        add(cash_date, investor_amount, source_ids)
    add(terminal_date, terminal_value, {item[0] for item in terminal_navs})
    cashflows = [
        (when, amount) for when, (amount, _) in sorted(grouped.items()) if amount
    ]
    status, reason, rate = _classify_independent_mwr(cashflows)
    return {
        "mwr_status": status,
        "mwr_reason": reason,
        "mwr_xirr": None if rate is None else _format_decimal(rate),
        "mwr_opening_date": opening_date.isoformat(),
        "mwr_opening_value": _format_decimal(opening_value),
        "mwr_terminal_date": terminal_date.isoformat(),
        "mwr_terminal_value": _format_decimal(terminal_value),
        "mwr_currency": currency,
        "mwr_coverage_status": "COMPLETE",
        "mwr_cashflows": [
            {
                "date": when.isoformat(),
                "amount": _format_decimal(amount),
                "source_record_ids": sorted(source_ids),
            }
            for when, (amount, source_ids) in sorted(grouped.items())
            if amount
        ],
        "mwr_label": (
            "STRATEGY_EPOCH_MWR" if basis == "STRATEGY_NAV" else "ACCOUNT_EPOCH_MWR"
        ),
        "mwr_value_basis": basis,
        "mwr_valuation_timing": "NAV_RECOGNITION_DATE_OR_EXACT_FILL_DATE",
        "mwr_day_count": "ACTUAL_365",
        "mwr_unit": "ANNUALIZED_RETURN",
        "mwr_source_binding": "GOVERNED_SOURCE_IDENTITIES",
    }


_PERIOD_YEAR_DAYS: Final[int] = 365


def _period_dec(value: object) -> Decimal | None:
    if value is None:
        return None
    return value if type(value) is Decimal else Decimal(str(value))


def _period_fmt(value: Decimal | None) -> str | None:
    return None if value is None else _format_decimal(value)


def _period_ppy(days: list[date]) -> tuple[int | None, str]:
    """Series cadence from its own dates: median gap, then weekend share."""

    if len(days) < 2:
        return None, "UNCLASSIFIED_TOO_FEW_OBSERVATIONS"
    gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
    gaps.sort()
    middle = gaps[len(gaps) // 2]
    if 5 <= middle <= 9:
        return 52, "WEEKLY"
    if middle < 4:
        if 5 * len([d for d in days if d.isoweekday() > 5]) >= len(days):
            return 365, "DAILY_CALENDAR"
        return 252, "DAILY_TRADING"
    return None, "UNCLASSIFIED_CADENCE"


def _independent_period_fields(
    numeric: Mapping[str, object], mwr: Mapping[str, object]
) -> dict[str, object]:
    """Derive the schema-10 period-return fields from the daily link path."""

    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        trace = numeric.get("observation_trace")
        links = trace["links"] if isinstance(trace, dict) else []
        if type(links) is not list:
            _fail()
        wealth_by_year: dict[int, Decimal] = {}
        first_by_year: dict[int, date] = {}
        last_by_year: dict[int, date] = {}
        ends: list[date] = []
        for link in links:
            opened = _parse_canonical_date(link["start_date"])
            closed = _parse_canonical_date(link["end_date"])
            ends.append(closed)
            first_by_year.setdefault(closed.year, opened)
            last_by_year[closed.year] = closed
            wealth_by_year[closed.year] = wealth_by_year.get(
                closed.year, Decimal(1)
            ) * Decimal(str(link["factor"]))
        start = _parse_canonical_date(links[0]["start_date"]) if links else None
        close = ends[-1] if ends else None
        days = None if start is None or close is None else (close - start).days
        twr = _period_dec(numeric.get("observation_linked_return"))
        annualised = None
        if (
            twr is not None
            and days is not None
            and days >= _PERIOD_YEAR_DAYS
            and twr + 1 > 0
        ):
            annualised = (twr + 1) ** (Decimal(_PERIOD_YEAR_DAYS) / Decimal(days)) - 1
        windows = {
            str(year): {
                "start": first_by_year[year].isoformat(),
                "end": last_by_year[year].isoformat(),
                "days": (last_by_year[year] - first_by_year[year]).days,
                "covers_period": first_by_year[year] <= date(year - 1, 12, 31),
            }
            for year in sorted(wealth_by_year)
        }
        yearly = {
            str(year): _format_decimal(wealth_by_year[year] - 1)
            for year in sorted(wealth_by_year)
        }
        key = None if close is None else str(close.year)
        ppy, ppy_basis = _period_ppy(ends)
        rate = _period_dec(mwr.get("mwr_xirr"))
        opened_text = mwr.get("mwr_opening_date")
        closed_text = mwr.get("mwr_terminal_date")
        mwr_days = None
        equivalent = None
        if opened_text is not None and closed_text is not None:
            mwr_days = (
                _parse_canonical_date(closed_text) - _parse_canonical_date(opened_text)
            ).days
            if rate is not None and mwr_days > 0 and rate + 1 > 0:
                equivalent = (rate + 1) ** (Decimal(mwr_days) / Decimal(365)) - 1
        span = (
            _parse_canonical_date(numeric["end"])
            - _parse_canonical_date(numeric["opening_capital_date"])
        ).days

        def label(length: int | None) -> str | None:
            if length is None:
                return None
            return (
                "ANNUALISED" if length >= _PERIOD_YEAR_DAYS else "ANNUALISED_FROM_DAYS"
            )

        if annualised is not None:
            status = "AVAILABLE"
        elif days is not None and days < _PERIOD_YEAR_DAYS:
            status = "SUPPRESSED_UNDER_365D"
        else:
            status = "UNAVAILABLE"
        ronc_annual = numeric.get("annualized_return_on_net_capital")
        return {
            "return_window_start": None if start is None else start.isoformat(),
            "return_close_date": None if close is None else close.isoformat(),
            "window_days": days,
            "twr_annualised": _period_fmt(annualised),
            "ytd_twr": None if key is None else yearly[key],
            "ytd_window": None if key is None else windows[key],
            "calendar_year_twr": yearly,
            "calendar_year_windows": windows,
            "periods_per_year": ppy,
            "periods_per_year_basis": ppy_basis,
            "mwr_window_equivalent": _period_fmt(equivalent),
            "annualisation_basis": {
                "twr": {
                    "reported_basis": "CUMULATIVE",
                    "window_days": days,
                    "annualised_field": "twr_annualised",
                    "annualised_status": status,
                    "annualised_method": (
                        None if annualised is None else "GEOMETRIC_ACT_365"
                    ),
                },
                "mwr_xirr": {
                    "reported_basis": None if rate is None else label(mwr_days),
                    "window_days": mwr_days,
                    "method": "XIRR_ACT_365",
                    "window_equivalent_field": "mwr_window_equivalent",
                },
                "mwr_window_equivalent": {
                    "reported_basis": None if equivalent is None else "CUMULATIVE",
                    "window_days": mwr_days,
                    "method": "XIRR_TO_WINDOW_ACT_365",
                },
                "return_on_net_capital": {
                    "reported_basis": "CUMULATIVE",
                    "window_days": span,
                },
                "annualized_return_on_net_capital": {
                    "reported_basis": None if ronc_annual is None else label(span),
                    "window_days": span,
                    "method": (
                        "GEOMETRIC_ACT_365"
                        if span >= _PERIOD_YEAR_DAYS
                        else "LINEAR_ACT_365"
                    ),
                },
                "ytd_twr": {
                    "reported_basis": "CUMULATIVE",
                    "window_days": None if key is None else windows[key]["days"],
                },
                "calendar_year_twr": {
                    "reported_basis": "CUMULATIVE",
                    "year_attribution": "LINK_END_DATE_YEAR",
                },
            },
        }


def _internal_id(value: object, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or _INTERNAL_ID.fullmatch(value) is None:
        _fail()
    return value


def _semantic_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail()
    return hashlib.sha256(encoded).hexdigest()


def _manifest(snapshot: InputSnapshot) -> GenerationManifest:
    try:
        manifest = GenerationManifest.from_json_bytes(snapshot.contents)
    except (TypeError, ValueError):
        _fail()
    if manifest.to_json_bytes() != snapshot.contents:
        _fail()
    return manifest


def _snapshot_identity(snapshot: InputSnapshot) -> dict[str, object]:
    return {"sha256": snapshot.sha256, "size_bytes": snapshot.size_bytes}


def _bound_catalog(
    catalog: InputSnapshot,
    manifest_snapshot: InputSnapshot,
    *,
    role: str,
) -> tuple[str, dict[str, object]]:
    catalog_object = _strict_object(catalog.contents)
    catalog_date = _date_value(catalog_object.get("as_of_date"))
    schema_version_key = (
        "catalog_schema_version"
        if role == "provider_ledger"
        else "baseline_schema_version"
    )
    schema_version = catalog_object.get(schema_version_key)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 0
    ):
        _fail()
    manifest = _manifest(manifest_snapshot)
    if manifest.verification.value != "VERIFIED":
        _fail()
    expected_artifact_id = (
        "normalized/provider-ledger.json"
        if role == "provider_ledger"
        else "normalized/portfolio-baseline.json"
    )
    expected_schema_name = (
        "provider-ledger-catalog"
        if role == "provider_ledger"
        else "portfolio-baseline-catalog"
    )
    matching = [
        item for item in manifest.artifacts if item.artifact_id == expected_artifact_id
    ]
    if len(matching) != 1:
        _fail()
    artifact = matching[0]
    if (
        artifact.sha256 != catalog.sha256
        or artifact.size_bytes != catalog.size_bytes
        or artifact.as_of_date != catalog_date
        or artifact.schema_name != expected_schema_name
        or artifact.schema_version != str(schema_version)
    ):
        _fail()
    manifest_artifact_id = (
        "identity/provider-ledger-generation.json"
        if role == "provider_ledger"
        else "identity/portfolio-baseline-generation.json"
    )
    manifest_identity = SourceArtifact(
        artifact_id=manifest_artifact_id,
        sha256=manifest_snapshot.sha256,
        size_bytes=manifest_snapshot.size_bytes,
        as_of_date=manifest.as_of_date,
        schema_name="generation-manifest",
        schema_version="1",
    )
    return manifest.generation_id, {
        "generation_id": manifest.generation_id,
        "artifact": artifact.to_dict(),
        "generation_manifest": manifest_identity.to_dict(),
    }


def _selected_identity(
    selected: IndependentVerifierInputs,
) -> dict[str, object]:
    provider_generation_id, provider = _bound_catalog(
        selected.provider_ledger,
        selected.provider_ledger_manifest,
        role="provider_ledger",
    )
    baseline_generation_id, baseline = _bound_catalog(
        selected.baseline,
        selected.baseline_manifest,
        role="baseline",
    )
    result: dict[str, object] = {
        "schema_version": 1,
        "artifact_id": "selected-input-identities",
        "as_of_date": selected.generation_as_of_date.isoformat(),
        "provider_ledger": provider,
        "baseline": baseline,
        "flow_classification_policy": _snapshot_identity(
            selected.flow_classification_policy
        ),
        "retrieval_receipts": [
            {"receipt_id": receipt_id, **_snapshot_identity(snapshot)}
            for receipt_id, snapshot in selected.retrieval_receipts.items()
        ],
        "code_sources": {
            "epoch_producer": {
                "artifact_id": "tools/account_strategy_manifest.py",
                **_snapshot_identity(selected.epoch_producer_source),
            },
            "generation_builder": {
                "artifact_id": "tools/build_account_manifest_generation.py",
                **_snapshot_identity(selected.generation_builder_source),
            },
            "independent_verifier": {
                "artifact_id": "tools/verify_account_manifest_generation.py",
                **_snapshot_identity(selected.independent_verifier_source),
            },
        },
    }
    if (
        provider_generation_id != provider["generation_id"]
        or baseline_generation_id != baseline["generation_id"]
    ):
        _fail()
    if selected._is_mixed:
        result["receipt_authority"] = _snapshot_identity(
            _require_snapshot(selected.receipt_authority)
        )
        result["admission_policy"] = _snapshot_identity(
            _require_snapshot(selected.admission_policy)
        )
        result["manifest_schema"] = _snapshot_identity(
            _require_snapshot(selected.manifest_schema)
        )
        result["membership_census"] = _snapshot_identity(
            _require_snapshot(selected.membership_census)
        )
    if selected.current_epoch_crosswalk is not None:
        result["current_epoch_crosswalk"] = _snapshot_identity(
            selected.current_epoch_crosswalk
        )
    if selected.sleeve_facts:
        result["sleeve_facts"] = [
            _snapshot_identity(snapshot)
            for snapshot in sorted(selected.sleeve_facts, key=lambda item: item.sha256)
        ]
    return result


def _report_source_metadata(
    selected: IndependentVerifierInputs,
) -> tuple[dict[str, object], dict[str, object]]:
    """Build fresh report metadata from exactly the selected source snapshots."""

    if type(selected) is not IndependentVerifierInputs:
        _fail()
    try:
        # The verifier is a code artifact: it is dated by the selected inputs it
        # read, matching `selected_source_identities.as_of_date`.  Only the
        # report's own as_of_date follows the calculation boundary.
        generated_date = selected.generation_as_of_date.isoformat()
        verifier_artifact = _subject_artifact(
            selected.independent_verifier_source.contents,
            "tools/verify_account_manifest_generation.py",
            "independent-account-manifest-verifier",
            "1",
            generated_date,
        )
        selected_source_identities = _selected_identity(selected)
    except (AttributeError, TypeError, ValueError):
        _fail()
    return verifier_artifact, selected_source_identities


def _fact_assignment(
    account_id: object,
    raw_date: object,
    intervals: Mapping[str, tuple[tuple[date | None, date | None, int, str], ...]],
) -> int | None:
    account = _internal_id(account_id)
    fact_date = _date_value(raw_date)
    if account not in intervals:
        return None
    matches = [
        index
        for index, (start, end, _, _) in enumerate(intervals[account])
        if (start is None or start <= fact_date) and (end is None or fact_date <= end)
    ]
    if len(matches) == 2:
        predecessor = intervals[account][matches[0]]
        successor = intervals[account][matches[1]]
        if (
            predecessor[3] == "HISTORICAL_POOL_ONLY"
            and successor[3] == "STRATEGY_MAPPED"
            and predecessor[0] is not None
            and predecessor[1] == successor[0] == fact_date
        ):
            matches = [matches[1]]
    if not matches:
        return None
    if len(matches) != 1:
        _fail()
    return matches[0]


def _validate_interval_rows(assignments: object) -> None:
    """Validate per-account interval ordering, overlap, and gap closure."""

    if not isinstance(assignments, list):
        _fail()
    grouped: dict[str, list[tuple[date | None, date | None, str]]] = {}
    for assignment in assignments:
        if not isinstance(assignment, dict):
            _fail()
        account_id = _internal_id(assignment.get("account_id"))
        disposition = assignment.get("strategy_mapping_disposition")
        if disposition not in {
            "STRATEGY_MAPPED",
            "HISTORICAL_POOL_ONLY",
            "RESERVE_NOT_APPLICABLE",
        }:
            _fail()
        start_value = assignment.get("effective_start")
        if start_value is None:
            if disposition == "STRATEGY_MAPPED":
                _fail()
            start = None
        else:
            start = _date_value(start_value)
        end_value = assignment.get("effective_end")
        end = None if end_value is None else _date_value(end_value)
        if start is not None and end is not None and start > end:
            _fail()
        grouped.setdefault(account_id, []).append((start, end, disposition))
    for intervals in grouped.values():
        ordered = sorted(
            intervals, key=lambda value: (value[0] is not None, value[0] or date.min)
        )
        for previous, current in zip(ordered, ordered[1:]):
            previous_start, previous_end, previous_disposition = previous
            current_start, _, current_disposition = current
            sanctioned_shared_day = (
                previous_disposition == "HISTORICAL_POOL_ONLY"
                and current_disposition == "STRATEGY_MAPPED"
                and previous_start is not None
                and previous_end is not None
                and previous_end == current_start
            )
            if sanctioned_shared_day:
                continue
            if (
                previous_end is None
                or current_start is None
                or current_start != previous_end + timedelta(days=1)
            ):
                _fail()


def _mapped_assignment_identity(perimeter: object) -> dict[str, object]:
    """Recompute the mapped-assignment identity without trusting any subject."""

    if not isinstance(perimeter, dict) or not isinstance(perimeter.get("rows"), list):
        _fail()
    records: list[dict[str, object]] = []
    for row in perimeter["rows"]:
        if not isinstance(row, dict):
            _fail()
        disposition = row.get("strategy_mapping_disposition")
        if disposition == "STRATEGY_MAPPED":
            account_id = _internal_id(row.get("account_id"))
            strategy_id = _internal_id(row.get("strategy_id"))
            effective_start = _date_value(row.get("effective_start"))
            end_value = row.get("effective_end")
            effective_end = None if end_value is None else _date_value(end_value)
            records.append(
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "effective_start": effective_start.isoformat(),
                    "effective_end": (
                        None if effective_end is None else effective_end.isoformat()
                    ),
                }
            )
    records.sort(
        key=lambda row: (
            row["account_id"],
            row["strategy_id"],
            row["effective_start"],
            row["effective_end"] or "9999-12-31",
        )
    )
    return {"count": len(records), "sha256": _semantic_digest(records)}


def _sorted_mapped_assignment_rows(rows: object) -> list[dict[str, object]]:
    if not isinstance(rows, list):
        _fail()
    mapped = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
    ]
    mapped.sort(
        key=lambda row: (
            str(row["account_id"]),
            str(row["strategy_id"]),
            str(row["effective_start"]),
            str(row["effective_end"]),
        )
    )
    return mapped


def _selected_current_epoch_rows(
    selected: IndependentVerifierInputs, rows: object
) -> list[dict[str, object]]:
    """Keep the all-assignment perimeter separate from the current subject set."""

    mapped = _sorted_mapped_assignment_rows(rows)
    if selected.current_epoch_crosswalk is None:
        return mapped
    included = _independently_validate_current_epoch_crosswalk(
        selected.baseline, selected.current_epoch_crosswalk
    )
    fields = ("account_id", "strategy_id", "effective_start", "effective_end")
    mapped_by_key: dict[tuple[object, ...], dict[str, object]] = {}
    for row in mapped:
        key = tuple(row.get(field) for field in fields)
        if key in mapped_by_key:
            _fail()
        mapped_by_key[key] = row
    result: list[dict[str, object]] = []
    included_keys: set[tuple[object, ...]] = set()
    view = _independent_sleeve_view(selected)
    left_strategy_set = 0
    for row in included:
        key = tuple(row.get(field) for field in fields)
        if (
            view is not None
            and row.get("account_id") in view.residual
            and key not in mapped_by_key
        ):
            # A residual parent's epoch became a non-strategy row.
            left_strategy_set += 1
            continue
        if key in included_keys or key not in mapped_by_key:
            _fail()
        included_keys.add(key)
        result.append(mapped_by_key[key])
    if len(result) + left_strategy_set != len(included):
        _fail()
    if view is not None:
        derived = [
            row for row in mapped if row.get("account_id") in view.sleeve_accounts
        ]
        derived.sort(
            key=lambda row: (str(row["account_id"]), str(row["effective_start"]))
        )
        result.extend(derived)
    return result


def _current_epoch_calculation_cutoff(
    selected: IndependentVerifierInputs,
) -> date | None:
    if selected.current_epoch_crosswalk is None:
        return None
    _independently_validate_current_epoch_crosswalk(
        selected.baseline, selected.current_epoch_crosswalk
    )
    crosswalk = _strict_object(selected.current_epoch_crosswalk.contents)
    return _date_value(crosswalk["calculation_cutoff_date"])


def _baseline_account_providers(accounts: object) -> dict[str, str]:
    if not isinstance(accounts, list):
        _fail()
    providers: dict[str, str] = {}
    for account in accounts:
        if not isinstance(account, dict) or set(account) != {
            "account_id",
            "base_currency",
            "provider",
            "role",
        }:
            _fail()
        account_id = _internal_id(account.get("account_id"))
        if account.get("base_currency") != "USD" or account.get("role") not in {
            "INVESTMENT",
            "RESERVE",
        }:
            _fail()
        provider = account.get("provider")
        if not isinstance(provider, str) or not provider or account_id in providers:
            _fail()
        providers[account_id] = provider
    return providers


def _perimeter_identity(
    selected: IndependentVerifierInputs,
    perimeter: dict[str, object],
    classified: dict[str, object],
) -> dict[str, object]:
    baseline = _strict_object(selected.baseline.contents)
    accounts = baseline.get("accounts")
    assignments = baseline.get("assignments")
    if not isinstance(accounts, list) or not isinstance(assignments, list):
        _fail()
    accounts, assignments = _effective_perimeter_lists(selected, accounts, assignments)
    account_by_id = _baseline_account_providers(accounts)
    rows: list[dict[str, object]] = []
    intervals: dict[str, list[tuple[date | None, date | None, int, str]]] = {}
    ownership_fields = {
        "account_id",
        "strategy_id",
        "strategy_mapping_disposition",
        "effective_start",
        "effective_end",
    }
    provenance_fields = {
        "assignment_id",
        "mapping_status",
        "observed_label",
        "pool_id",
        "pool_mapping_status",
        "source_classification",
        "source_id",
    }
    assignment_ids: set[str] = set()
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            _fail()
        assignment_fields = frozenset(assignment)
        if assignment_fields not in {
            frozenset(ownership_fields),
            frozenset(ownership_fields | {"assignment_id"}),
            frozenset(ownership_fields | provenance_fields),
        }:
            _fail()
        account_id = _internal_id(assignment.get("account_id"))
        if account_id not in account_by_id:
            _fail()
        disposition = assignment.get("strategy_mapping_disposition")
        if disposition not in {
            "STRATEGY_MAPPED",
            "HISTORICAL_POOL_ONLY",
            "RESERVE_NOT_APPLICABLE",
        }:
            _fail()
        if "assignment_id" in assignment:
            assignment_id = assignment["assignment_id"]
            if (
                not isinstance(assignment_id, str)
                or not assignment_id
                or assignment_id in assignment_ids
            ):
                _fail()
            assignment_ids.add(assignment_id)
        if provenance_fields <= set(assignment):
            text_fields = (
                "assignment_id",
                "observed_label",
                "pool_id",
                "source_classification",
                "source_id",
            )
            if any(
                not isinstance(assignment.get(field), str) or not assignment[field]
                for field in text_fields
            ):
                _fail()
            expected_mapping_status = (
                "MAPPED" if disposition == "STRATEGY_MAPPED" else "UNMAPPED"
            )
            if (
                assignment.get("mapping_status") != expected_mapping_status
                or assignment.get("pool_mapping_status") != "MAPPED"
            ):
                _fail()
        strategy = _internal_id(assignment.get("strategy_id"), nullable=True)
        if (disposition == "STRATEGY_MAPPED") != (strategy is not None):
            _fail()
        start_value = assignment.get("effective_start")
        if start_value is None:
            if disposition == "STRATEGY_MAPPED":
                _fail()
            start = None
        else:
            start = _date_value(start_value)
        end_value = assignment.get("effective_end")
        end = None if end_value is None else _date_value(end_value)
        if start is not None and end is not None and start > end:
            _fail()
        interval_list = intervals.setdefault(account_id, [])
        interval_list.append((start, end, index, disposition))
        rows.append(
            {
                "account_id": account_id,
                "strategy_id": strategy,
                "strategy_mapping_disposition": disposition,
                "effective_start": None if start is None else start.isoformat(),
                "effective_end": None if end is None else end.isoformat(),
            }
        )
    if not intervals or not set(intervals) <= set(account_by_id):
        _fail()
    _validate_interval_rows(assignments)
    rows.sort(
        key=lambda row: (
            row["account_id"],
            row["effective_start"] is not None,
            row["effective_start"] or "",
            row["effective_end"] or "9999-12-31",
        )
    )
    ordered_intervals: dict[
        str, tuple[tuple[date | None, date | None, int, str], ...]
    ] = {}
    for account, values in intervals.items():
        ordered = sorted(
            values, key=lambda value: (value[0] is not None, value[0] or date.min)
        )
        ordered_intervals[account] = tuple(ordered)
    ledger = _ledger_with_derived(selected)
    nav_facts = ledger.get("nav_observations")
    raw_flows = ledger.get("flows")
    ledger_as_of_date = _validate_provider_ledger_date_authority(selected, ledger)
    events = classified.get("events")
    if (
        not isinstance(nav_facts, list)
        or not isinstance(raw_flows, list)
        or not isinstance(events, list)
    ):
        _fail()
    nav_ids: set[str] = set()
    nav_account_dates: set[tuple[str, str]] = set()
    raw_ids: set[str] = set()
    event_ids: set[str] = set()
    event_sources: set[str] = set()
    raw_by_id: dict[str, tuple[str, str, int | None]] = {}
    unassigned_facts: list[dict[str, object]] = []
    general_facts: list[dict[str, object]] = []
    post_end_facts: list[dict[str, object]] = []
    crosswalk_dispositions: dict[tuple[str, str, str, str], str] = {}
    if selected.current_epoch_crosswalk is not None:
        crosswalk_rows = _strict_object(selected.current_epoch_crosswalk.contents).get(
            "rows"
        )
        if not isinstance(crosswalk_rows, list):
            _fail()
        for crosswalk_row in crosswalk_rows:
            if not isinstance(crosswalk_row, dict):
                _fail()
            crosswalk_dispositions[
                (
                    str(crosswalk_row["account_id"]),
                    str(crosswalk_row["strategy_id"]),
                    str(crosswalk_row["effective_start"]),
                    str(crosswalk_row["effective_end"]),
                )
            ] = str(crosswalk_row["product_epoch_disposition"])

    def post_end_target(account: str, day: str) -> tuple[str, str] | None:
        chain = [
            (start, end, index)
            for start, end, index, disposition in ordered_intervals.get(account, ())
            if disposition == "STRATEGY_MAPPED"
        ]
        if not chain or any(end is None for _start, end, _index in chain):
            return None
        start, end, index = max(chain, key=lambda item: (item[1], item[0]))
        if start is None or end is None or day <= end.isoformat():
            return None
        strategy = str(assignments[index]["strategy_id"])
        product = crosswalk_dispositions.get(
            (account, strategy, start.isoformat(), end.isoformat())
        )
        return None if product is None else (strategy, product)

    def general_reason(account: str, day: str) -> str | None:
        # Re-derived from the account's STRATEGY_MAPPED intervals alone.
        mapped = [
            (start, end)
            for start, end, _index, disposition in ordered_intervals.get(account, ())
            if disposition == "STRATEGY_MAPPED"
        ]
        if not mapped:
            return (
                "NO_STRATEGY_ASSIGNED_TO_ACCOUNT"
                if account in account_by_id
                else "ACCOUNT_NOT_IN_SELECTED_BASELINE"
            )
        starts = [start.isoformat() for start, _end in mapped if start is not None]
        if starts and day < min(starts):
            return "PREDATES_FIRST_STRATEGY_ASSIGNMENT"
        return None

    def file_unassigned(fact: dict[str, object], account: str, day: str) -> None:
        reason = general_reason(account, day)
        if reason is not None:
            general_facts.append(
                {
                    **fact,
                    "status": "GENERAL",
                    "disposition": "GENERAL_OUT_OF_STRATEGY_PERIMETER",
                    "reason": reason,
                }
            )
            return
        target = post_end_target(account, day)
        if target is not None:
            strategy, product = target
            post_end_facts.append(
                {
                    **fact,
                    "status": "POST_END",
                    "disposition": (
                        "POST_END_TAIL_ATTRIBUTED_TO_STRATEGY"
                        if product == "INCLUDED_PRODUCT_EPOCH"
                        else "POST_END_HISTORY_EXCLUDED_BY_PRODUCT_DISPOSITION"
                    ),
                    "strategy_id": strategy,
                    "product_epoch_disposition": product,
                }
            )
            return
        unassigned_facts.append(
            {
                **fact,
                "status": "BLOCKED",
                "blocker_code": (
                    "NO_ASSIGNMENT_MATCH"
                    if account in account_by_id
                    else "ACCOUNT_NOT_IN_SELECTED_BASELINE"
                ),
            }
        )

    counts: dict[int, list[int]] = {
        index: [0, 0, 0, 0] for index in range(len(assignments))
    }
    for nav in nav_facts:
        if (
            not isinstance(nav, dict)
            or not isinstance(nav.get("record_id"), str)
            or not nav["record_id"]
            or nav["record_id"] in nav_ids
        ):
            _fail()
        nav_ids.add(nav["record_id"])
        nav_account = _internal_id(nav.get("account_id"))
        nav_date = _date_value(nav.get("as_of_date"))
        _reject_provider_fact_after_ledger_date(nav_date, ledger_as_of_date)
        nav_key = (nav_account, nav_date.isoformat())
        if nav_key in nav_account_dates:
            _fail()
        nav_account_dates.add(nav_key)
        amount = nav.get("nav")
        if not isinstance(amount, dict) or amount.get("status") not in {
            "AVAILABLE",
            "UNAVAILABLE",
        }:
            _fail()
        index = _fact_assignment(nav_account, nav_date.isoformat(), ordered_intervals)
        if index is None:
            file_unassigned(
                {
                    "source_kind": "NAV",
                    "source_identity": {"record_id": nav["record_id"]},
                    "account_id": nav_account,
                    "fact_date": nav_date.isoformat(),
                    "assignment_match_count": 0,
                },
                nav_account,
                nav_date.isoformat(),
            )
        else:
            assignment_index = ordered_intervals[nav_account][index][2]
            counts[assignment_index][0 if amount["status"] == "AVAILABLE" else 1] += 1
    for flow in raw_flows:
        if (
            not isinstance(flow, dict)
            or not isinstance(flow.get("record_id"), str)
            or not flow["record_id"]
            or flow["record_id"] in raw_ids
        ):
            _fail()
        raw_ids.add(flow["record_id"])
        flow_account = _internal_id(flow.get("account_id"))
        flow_date = _date_value(flow.get("event_date")).isoformat()
        _reject_provider_fact_after_ledger_date(
            _date_value(flow.get("event_date")), ledger_as_of_date
        )
        index = _fact_assignment(flow_account, flow_date, ordered_intervals)
        raw_by_id[flow["record_id"]] = (flow_account, flow_date, index)
        if index is not None:
            assignment_index = ordered_intervals[flow_account][index][2]
            counts[assignment_index][2] += 1
    for event in events:
        if (
            not isinstance(event, dict)
            or not isinstance(event.get("event_id"), str)
            or not event["event_id"]
            or event["event_id"] in event_ids
        ):
            _fail()
        source_id = event.get("source_record_id")
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id in event_sources
            or source_id not in raw_ids
        ):
            _fail()
        event_ids.add(event["event_id"])
        event_sources.add(source_id)
        event_account = _internal_id(event.get("account_id"))
        event_date = _date_value(event.get("event_date")).isoformat()
        _reject_provider_fact_after_ledger_date(
            _date_value(event.get("event_date")), ledger_as_of_date
        )
        index = _fact_assignment(event_account, event_date, ordered_intervals)
        raw_account, raw_date, raw_index = raw_by_id[source_id]
        if event_account != raw_account or event_date != raw_date or index != raw_index:
            _fail()
        if index is None:
            file_unassigned(
                {
                    "source_kind": "RAW_FLOW",
                    "source_identity": {"record_id": source_id},
                    "classified_projection_identity": {
                        "event_id": event["event_id"],
                        "source_record_id": source_id,
                    },
                    "account_id": event_account,
                    "fact_date": event_date,
                    "assignment_match_count": 0,
                },
                event_account,
                event_date,
            )
        else:
            assignment_index = ordered_intervals[event_account][index][2]
            counts[assignment_index][3] += 1
    if event_sources != raw_ids:
        _fail()
    for sortable in (unassigned_facts, general_facts, post_end_facts):
        sortable.sort(
            key=lambda item: (
                0 if item["source_kind"] == "NAV" else 1,
                str(item["account_id"]),
                str(item["fact_date"]),
                str(item["source_identity"]["record_id"]),
            )
        )
    post_end_summaries: list[dict[str, object]] = []
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for fact in post_end_facts:
        grouped.setdefault(
            (
                str(fact["strategy_id"]),
                str(fact["disposition"]),
                str(fact["product_epoch_disposition"]),
            ),
            [],
        ).append(fact)
    for (strategy, disposition, product), members in sorted(grouped.items()):
        fact_dates = sorted(str(fact["fact_date"]) for fact in members)
        post_end_summaries.append(
            {
                "strategy_id": strategy,
                "disposition": disposition,
                "product_epoch_disposition": product,
                "nav_fact_count": sum(fact["source_kind"] == "NAV" for fact in members),
                "raw_flow_fact_count": sum(
                    fact["source_kind"] == "RAW_FLOW" for fact in members
                ),
                "first_fact_date": fact_dates[0],
                "last_fact_date": fact_dates[-1],
            }
        )
    for row in rows:
        assignment_key = (
            row["account_id"],
            row["effective_start"],
            row["effective_end"],
        )
        assignment_index = next(
            index
            for index, assignment in enumerate(assignments)
            if assignment["account_id"] == assignment_key[0]
            and assignment["effective_start"] == assignment_key[1]
            and assignment["effective_end"] == assignment_key[2]
        )
        row_counts = counts[assignment_index]
        row.update(
            {
                "provider": account_by_id[row["account_id"]],
                "calculation_eligible": row["strategy_mapping_disposition"]
                == "STRATEGY_MAPPED",
                "available_nav": row_counts[0],
                "unavailable_nav": row_counts[1],
                "raw_flows": row_counts[2],
                "classified_flows": row_counts[3],
            }
        )
        row["assignment_identity"] = {
            "sha256": _semantic_digest(
                {
                    key: row[key]
                    for key in (
                        "account_id",
                        "strategy_id",
                        "strategy_mapping_disposition",
                        "effective_start",
                        "effective_end",
                    )
                }
            )
        }
    expected = {
        "schema_version": 1,
        "artifact_id": "account-assignment-perimeter",
        "as_of_date": selected.generation_as_of_date.isoformat(),
        "selected_baseline_identity": {
            "generation_id": _selected_identity(selected)["baseline"]["generation_id"],
            "artifact": _selected_identity(selected)["baseline"]["artifact"],
        },
        "full_perimeter_identity": {
            "count": len(rows),
            "sha256": _semantic_digest(
                [
                    {
                        key: row[key]
                        for key in (
                            "account_id",
                            "strategy_id",
                            "strategy_mapping_disposition",
                            "effective_start",
                            "effective_end",
                        )
                    }
                    for row in rows
                ]
            ),
        },
        "selected_baseline_account_count": len(account_by_id),
        "account_count": len(intervals),
        "assignment_count": len(rows),
        "partition_counts": {
            disposition: sum(
                row["strategy_mapping_disposition"] == disposition for row in rows
            )
            for disposition in (
                "STRATEGY_MAPPED",
                "HISTORICAL_POOL_ONLY",
                "RESERVE_NOT_APPLICABLE",
            )
        },
        "fact_bijection_status": (
            "BLOCKED_UNASSIGNED_FACTS" if unassigned_facts else "ONE_TO_ONE_VERIFIED"
        ),
        "unassigned_fact_count": len(unassigned_facts),
        "unassigned_projection_count": sum(
            2 if fact["source_kind"] == "RAW_FLOW" else 1 for fact in unassigned_facts
        ),
        "unassigned_facts": unassigned_facts,
        **(
            {"general_fact_count": len(general_facts), "general_facts": general_facts}
            if general_facts
            else {}
        ),
        **(
            {
                "post_end_fact_count": len(post_end_facts),
                "post_end_facts": post_end_facts,
                "post_end_summaries": post_end_summaries,
            }
            if post_end_facts
            else {}
        ),
        "rows": rows,
    }
    if perimeter != expected:
        _fail()
    return expected


def _verify_c4b1_contract(
    selected_snapshots: IndependentVerifierInputs,
    subject_payloads: Mapping[object, object],
) -> dict[str, object]:
    normalized = _normalize_subject_payloads(subject_payloads)
    return _verify_c4b1_contract_normalized(selected_snapshots, normalized)


def _verify_c4b1_contract_normalized(
    selected_snapshots: IndependentVerifierInputs,
    normalized: Mapping[str, bytes],
) -> dict[str, object]:
    selected_identity = _strict_object(
        normalized["authority/selected-input-identities.json"]
    )
    expected_identity = _selected_identity(selected_snapshots)
    if selected_identity != expected_identity:
        _fail()
    if selected_snapshots.current_epoch_crosswalk is not None:
        if (
            normalized.get(_CROSSWALK_SUBJECT_PATH)
            != selected_snapshots.current_epoch_crosswalk.contents
        ):
            _fail()
        _independently_validate_current_epoch_crosswalk(
            selected_snapshots.baseline,
            selected_snapshots.current_epoch_crosswalk,
            baseline_generation_id=expected_identity["baseline"]["generation_id"],
        )
    perimeter = _strict_object(
        normalized["authority/account-assignment-perimeter.json"]
    )
    classified = _strict_object(normalized["authority/classified-flows.json"])
    if classified.get("artifact_id") != "authority/classified-flows.json":
        _fail()
    if (
        perimeter.get("as_of_date")
        != selected_snapshots.generation_as_of_date.isoformat()
    ):
        _fail()
    _perimeter_identity(selected_snapshots, perimeter, classified)
    selected_rows = _selected_current_epoch_rows(
        selected_snapshots, perimeter.get("rows")
    )
    return {
        "mapped_assignments_identity": _mapped_assignment_identity(
            {"rows": selected_rows}
        )
    }


def _classification_rules(
    selected_policy: InputSnapshot,
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]], dict[str, object]]:
    policy = _strict_object(selected_policy.contents)
    if (
        set(policy)
        != {
            "schema_version",
            "artifact_id",
            "as_of_date",
            "policy_id",
            "policy_version",
            "event_dispositions",
        }
        or isinstance(policy.get("schema_version"), bool)
        or policy.get("schema_version") != 1
    ):
        _fail()
    _date_value(policy.get("as_of_date"))
    if not isinstance(policy.get("artifact_id"), str) or not policy["artifact_id"]:
        _fail()
    for key in ("policy_id", "policy_version"):
        if not isinstance(policy.get(key), str) or not policy[key]:
            _fail()
    dispositions = policy.get("event_dispositions")
    if not isinstance(dispositions, dict) or set(dispositions) != {"exact", "prefix"}:
        _fail()
    exact_rows = dispositions.get("exact")
    prefix_rows = dispositions.get("prefix")
    if not isinstance(exact_rows, list) or not isinstance(prefix_rows, list):
        _fail()
    exact: dict[str, dict[str, str]] = {}
    prefixes: list[dict[str, str]] = []
    for row in exact_rows:
        if not isinstance(row, dict) or set(row) not in (
            {"flow_type", "disposition", "amount_sign"},
            {"flow_type", "disposition", "amount_sign", "counterparty_rule"},
            {
                "flow_type",
                "disposition",
                "amount_sign",
                "counterparty_rule",
                "unpaired_disposition",
            },
        ):
            _fail()
        flow_type = row.get("flow_type")
        if not isinstance(flow_type, str) or not flow_type or flow_type in exact:
            _fail()
        disposition = row.get("disposition")
        sign = row.get("amount_sign")
        if disposition not in {"EXTERNAL", "INTERNAL"} or sign not in {
            "POSITIVE",
            "NEGATIVE",
            "NONZERO",
        }:
            _fail()
        if disposition == "INTERNAL":
            counterparty_rule = row.get("counterparty_rule")
            if counterparty_rule not in {
                _COUNTERPARTY_RULE,
                _SOURCE_IDENTITY_COUNTERPARTY_RULE,
            } or (
                counterparty_rule == _SOURCE_IDENTITY_COUNTERPARTY_RULE
                and flow_type != "ACCOUNT_TRANSFER"
            ):
                _fail()
            if (
                "unpaired_disposition" in row
                and row["unpaired_disposition"] != "EXTERNAL"
            ):
                _fail()
        elif "counterparty_rule" in row:
            _fail()
        exact[flow_type] = {key: row[key] for key in row if key != "flow_type"}
    seen_prefixes: set[str] = set()
    for row in prefix_rows:
        if not isinstance(row, dict) or set(row) not in (
            {"flow_type_prefix", "disposition", "amount_sign"},
            {"flow_type_prefix", "disposition", "amount_sign", "counterparty_rule"},
            {
                "flow_type_prefix",
                "disposition",
                "amount_sign",
                "counterparty_rule",
                "unpaired_disposition",
            },
        ):
            _fail()
        prefix = row.get("flow_type_prefix")
        if not isinstance(prefix, str) or not prefix or prefix in seen_prefixes:
            _fail()
        disposition = row.get("disposition")
        sign = row.get("amount_sign")
        if disposition not in {"EXTERNAL", "INTERNAL"} or sign not in {
            "POSITIVE",
            "NEGATIVE",
            "NONZERO",
        }:
            _fail()
        if disposition == "INTERNAL":
            if row.get("counterparty_rule") != _COUNTERPARTY_RULE:
                _fail()
            if (
                "unpaired_disposition" in row
                and row["unpaired_disposition"] != "EXTERNAL"
            ):
                _fail()
        elif "counterparty_rule" in row:
            _fail()
        seen_prefixes.add(prefix)
        prefixes.append({key: row[key] for key in row})
    if not exact and not prefixes:
        _fail()
    for index, prefix in enumerate(seen_prefixes):
        if any(
            prefix.startswith(other) or other.startswith(prefix)
            for other in list(seen_prefixes)[index + 1 :]
        ):
            _fail()
    if any(
        flow_type.startswith(prefix) for flow_type in exact for prefix in seen_prefixes
    ):
        _fail()
    return exact, prefixes, policy


def _normalize_provider_navs(
    selected: IndependentVerifierInputs,
    *,
    enforce_global_accounting_currency: bool = True,
) -> tuple[list[dict[str, object]], str | None]:
    ledger = _ledger_with_derived(selected)
    ledger_as_of_date = _validate_provider_ledger_date_authority(selected, ledger)
    navs = ledger.get("nav_observations")
    flows = ledger.get("flows")
    if not isinstance(navs, list) or not isinstance(flows, list):
        _fail()
    normalized: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    available_currencies: set[str] = set()

    def normalize_money(
        value: object,
        *,
        allow_reason: bool,
        allow_asset_currency: bool = False,
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            _fail()
        base_keys = {"currency", "status", "value"}
        if set(value) != base_keys and not (
            allow_reason
            and set(value) == base_keys | {"reason"}
            and value.get("status") == "UNAVAILABLE"
        ):
            _fail()
        currency = value.get("currency")
        status = value.get("status")
        amount = value.get("value")
        currency_pattern = r"[A-Z]{3,12}" if allow_asset_currency else r"[A-Z]{3}"
        if (
            not isinstance(currency, str)
            or re.fullmatch(currency_pattern, currency) is None
        ):
            _fail()
        if status == "UNAVAILABLE":
            if amount is not None:
                _fail()
            result: dict[str, object] = {
                "currency": currency,
                "status": status,
                "value": None,
            }
            if "reason" in value:
                reason = value["reason"]
                if not isinstance(reason, str) or not reason.strip():
                    _fail()
                result["reason"] = reason.strip()
            return result
        if status != "AVAILABLE" or not isinstance(amount, str):
            _fail()
        _parse_retained_source_decimal(amount)
        return {"currency": currency, "status": status, "value": amount}

    for row in navs:
        if not isinstance(row, dict) or not {
            "record_id",
            "account_id",
            "as_of_date",
            "nav",
        } <= set(row):
            _fail()
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or not record_id or record_id in seen_ids:
            _fail()
        seen_ids.add(record_id)
        amount = normalize_money(row.get("nav"), allow_reason=True)
        account_id = _internal_id(row.get("account_id"))
        as_of_date = _date_value(row.get("as_of_date"))
        _reject_provider_fact_after_ledger_date(as_of_date, ledger_as_of_date)
        if amount["status"] == "AVAILABLE":
            available_currencies.add(str(amount["currency"]))
        normalized_nav: dict[str, object] = {
            "record_id": record_id,
            "account_id": account_id,
            "as_of_date": as_of_date.isoformat(),
            "nav": amount,
        }
        if "value_basis" in row:
            normalized_nav["value_basis"] = _fact_text(row.get("value_basis"))
        normalized.append(normalized_nav)
    if enforce_global_accounting_currency:
        if len(available_currencies) != 1:
            _fail()
        accounting_currency: str | None = next(iter(available_currencies))
    else:
        accounting_currency = None
    for flow in flows:
        if not isinstance(flow, dict):
            _fail()
        event_date = _date_value(flow.get("event_date"))
        _date_value(flow.get("nav_recognition_date", flow.get("event_date")))
        _reject_provider_fact_after_ledger_date(event_date, ledger_as_of_date)
        amount = normalize_money(
            flow.get("amount"),
            allow_reason=False,
            allow_asset_currency=not enforce_global_accounting_currency,
        )
        if (
            enforce_global_accounting_currency
            and amount["status"] == "AVAILABLE"
            and amount["currency"] != accounting_currency
        ):
            _fail()
    cutoff = _current_epoch_calculation_cutoff(selected)
    if cutoff is not None:
        normalized = [
            row for row in normalized if _date_value(row["as_of_date"]) <= cutoff
        ]
    return normalized, accounting_currency


def _verify_c4b3_calculation_inputs(
    selected: IndependentVerifierInputs,
    subject_payloads: Mapping[str, bytes],
    b1_state: Mapping[str, object],
) -> None:
    navs, accounting_currency = _normalize_provider_navs(selected)
    perimeter = _strict_object(
        subject_payloads["authority/account-assignment-perimeter.json"]
    )
    classified = _strict_object(subject_payloads["authority/classified-flows.json"])
    baseline = _strict_object(selected.baseline.contents)
    accounts = baseline.get("accounts")
    assignments = perimeter.get("rows")
    if not isinstance(accounts, list) or not isinstance(assignments, list):
        _fail()
    providers = _baseline_account_providers(accounts)
    mapped = _selected_current_epoch_rows(selected, assignments)
    events = classified.get("events")
    if not isinstance(events, list):
        _fail()
    cutoff = _current_epoch_calculation_cutoff(selected)
    if cutoff is not None:
        events = [
            event
            for event in events
            if isinstance(event, dict)
            and _date_value(event.get("nav_recognition_date", event.get("event_date")))
            <= cutoff
        ]

    def in_interval(value: str, row: Mapping[str, object]) -> bool:
        event_date = _date_value(value)
        start = _date_value(row["effective_start"])
        end_value = row["effective_end"]
        end = None if end_value is None else _date_value(end_value)
        return start <= event_date and (end is None or event_date <= end)

    expected_rows = []
    for row in mapped:
        if not isinstance(row, dict) or row.get("account_id") not in providers:
            _fail()
        expected_rows.append(
            {
                "epoch": {
                    key: row[key]
                    for key in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                },
                "provider": providers[row["account_id"]],
                "nav_observations": [
                    nav
                    for nav in navs
                    if nav["account_id"] == row["account_id"]
                    and in_interval(nav["as_of_date"], row)
                ],
                "classified_flows": [
                    event
                    for event in events
                    if isinstance(event, dict)
                    and event.get("account_id") == row["account_id"]
                    and in_interval(
                        event.get("nav_recognition_date", event.get("event_date")),
                        row,
                    )
                ],
            }
        )
    selected_identity = _selected_identity(selected)
    provider_identity = selected_identity["provider_ledger"]
    expected = {
        "schema_version": 1,
        "artifact_id": "authority/calculation-inputs.json",
        "as_of_date": selected.generation_as_of_date.isoformat(),
        "accounting_currency": accounting_currency,
        "ledger_identity": {
            "generation_id": provider_identity["generation_id"],
            "artifact": provider_identity["artifact"],
            "identity_artifact": provider_identity["generation_manifest"],
        },
        "baseline_identity": {
            "generation_id": selected_identity["baseline"]["generation_id"],
            "artifact": selected_identity["baseline"]["artifact"],
        },
        "mapped_assignments_identity": b1_state["mapped_assignments_identity"],
        "rows": expected_rows,
    }
    if (
        _strict_object(subject_payloads["authority/calculation-inputs.json"])
        != expected
    ):
        _fail()


def _verify_c4b3_code_identities(
    selected: IndependentVerifierInputs,
    subject_payloads: Mapping[str, bytes],
) -> None:
    selected_identity = _selected_identity(selected)
    expected = {
        "schema_version": 1,
        "artifact_id": "authority/code-identities.json",
        "as_of_date": selected.generation_as_of_date.isoformat(),
        "code_sources": selected_identity["code_sources"],
    }
    if _strict_object(subject_payloads["authority/code-identities.json"]) != expected:
        _fail()


def _subject_artifact(
    contents: bytes,
    artifact_id: str,
    schema_name: str,
    schema_version: str,
    as_of_date: str,
) -> dict[str, object]:
    return SourceArtifact(
        artifact_id=artifact_id,
        sha256=hashlib.sha256(contents).hexdigest(),
        size_bytes=len(contents),
        as_of_date=_date_value(as_of_date),
        schema_name=schema_name,
        schema_version=schema_version,
    ).to_dict()


def _verify_c4b3_2a_coverage_admission(
    selected: IndependentVerifierInputs,
    subject_payloads: Mapping[str, bytes],
    b1_state: Mapping[str, object],
) -> None:
    generated_date = selected.generation_as_of_date.isoformat()
    provider_ledger_date = _selected_date(selected.provider_ledger)
    selected_identity = _selected_identity(selected)
    ledger_identity = {
        "generation_id": selected_identity["provider_ledger"]["generation_id"],
        "artifact": selected_identity["provider_ledger"]["artifact"],
    }
    perimeter = _strict_object(
        subject_payloads["authority/account-assignment-perimeter.json"]
    )
    perimeter_rows = perimeter.get("rows")
    if not isinstance(perimeter_rows, list):
        _fail()
    mapped = _selected_current_epoch_rows(selected, perimeter_rows)
    classified_bytes = subject_payloads["authority/classified-flows.json"]
    classified_artifact = _subject_artifact(
        classified_bytes,
        "authority/classified-flows.json",
        "classified-flows",
        "4",
        generated_date,
    )
    coverage_rows: list[dict[str, object]] = []
    for row in mapped:
        start = _date_value(row["effective_start"])
        if start > provider_ledger_date:
            _fail()
        end_value = row["effective_end"]
        if end_value is None:
            end = provider_ledger_date
        else:
            end = _date_value(end_value)
            if end > provider_ledger_date:
                _fail()
        coverage_rows.append(
            {
                "assignment": {
                    key: row[key]
                    for key in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                },
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "status": "BLOCKED",
                "authority_kind": "NONE",
                "receipt_artifact_id": None,
                "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
                "blocker_detail": "No admissible retained receipt issuer.",
            }
        )
    coverage_expected = {
        "schema_version": 2,
        "artifact_id": "authority/account-flow-coverage.json",
        "as_of_date": provider_ledger_date.isoformat(),
        "ledger_identity": ledger_identity,
        "classified_flows_artifact": classified_artifact,
        "mapped_assignments_identity": b1_state["mapped_assignments_identity"],
        "receipt_artifacts": [],
        "rows": coverage_rows,
    }
    coverage_bytes = subject_payloads["authority/account-flow-coverage.json"]
    if _strict_object(coverage_bytes) != coverage_expected:
        _fail()
    coverage_artifact = _subject_artifact(
        coverage_bytes,
        "authority/account-flow-coverage.json",
        "account-flow-coverage",
        "2",
        provider_ledger_date.isoformat(),
    )
    admission_expected = {
        "schema_version": 1,
        "artifact_id": "authority/mapped-epoch-admission.json",
        "as_of_date": generated_date,
        "ledger_identity": ledger_identity,
        "coverage_artifact": coverage_artifact,
        "mapped_assignments_identity": b1_state["mapped_assignments_identity"],
        "rows": [
            {
                "assignment": {
                    key: row[key]
                    for key in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                },
                "status": "BLOCKED",
                "authority_kind": "NONE",
                "funded_nav_record_ids": [],
                "continuity_to_successor": False,
                "blocker_code": "FLOW_COMPLETENESS_NOT_PROVEN",
            }
            for row in mapped
        ],
    }
    if (
        _strict_object(subject_payloads["authority/mapped-epoch-admission.json"])
        != admission_expected
    ):
        _fail()


def _verify_c4b3_2b1_output(
    selected: IndependentVerifierInputs,
    subject_payloads: Mapping[str, bytes],
    b1_state: Mapping[str, object],
) -> None:
    selected_identity = _selected_identity(selected)
    provider_identity = selected_identity["provider_ledger"]
    ledger_source = {
        "generation_id": provider_identity["generation_id"],
        "artifact": provider_identity["artifact"],
        "identity_artifact": provider_identity["generation_manifest"],
    }
    baseline_source = {
        "generation_id": selected_identity["baseline"]["generation_id"],
        "artifact": selected_identity["baseline"]["artifact"],
    }
    generated_date = selected.generation_as_of_date.isoformat()
    classified_artifact = _subject_artifact(
        subject_payloads["authority/classified-flows.json"],
        "authority/classified-flows.json",
        "classified-flows",
        "4",
        generated_date,
    )
    policy_payload = _strict_object(selected.flow_classification_policy.contents)
    policy_bytes = canonical_json_bytes(policy_payload)
    policy_artifact = _subject_artifact(
        policy_bytes,
        "authority/flow-classification-policy.json",
        "flow-classification-policy",
        "1",
        str(policy_payload.get("as_of_date")),
    )
    policy_identity = {
        "policy_id": policy_payload.get("policy_id"),
        "policy_version": policy_payload.get("policy_version"),
        "event_dispositions": policy_payload.get("event_dispositions"),
        "artifact": policy_artifact,
    }
    coverage_artifact = _subject_artifact(
        subject_payloads["authority/account-flow-coverage.json"],
        "authority/account-flow-coverage.json",
        "account-flow-coverage",
        "2",
        _selected_date(selected.provider_ledger).isoformat(),
    )
    admission_artifact = _subject_artifact(
        subject_payloads["authority/mapped-epoch-admission.json"],
        "authority/mapped-epoch-admission.json",
        "mapped-epoch-admission",
        "1",
        generated_date,
    )
    selected_artifact = _subject_artifact(
        subject_payloads["authority/selected-input-identities.json"],
        "authority/selected-input-identities.json",
        "selected-input-identities",
        "1",
        generated_date,
    )
    perimeter_artifact = _subject_artifact(
        subject_payloads["authority/account-assignment-perimeter.json"],
        "authority/account-assignment-perimeter.json",
        "account-assignment-perimeter",
        "1",
        generated_date,
    )
    calculation_artifact = _subject_artifact(
        subject_payloads["authority/calculation-inputs.json"],
        "authority/calculation-inputs.json",
        "account-manifest-calculation-inputs",
        "1",
        generated_date,
    )
    code_artifact = _subject_artifact(
        subject_payloads["authority/code-identities.json"],
        "authority/code-identities.json",
        "account-manifest-code-identities",
        "1",
        generated_date,
    )
    baseline = _strict_object(selected.baseline.contents)
    accounts = baseline.get("accounts")
    perimeter = _strict_object(
        subject_payloads["authority/account-assignment-perimeter.json"]
    )
    rows = perimeter.get("rows")
    if not isinstance(accounts, list) or not isinstance(rows, list):
        _fail()
    providers = _baseline_account_providers(accounts)
    mapped = _selected_current_epoch_rows(selected, rows)
    display_by_epoch = (
        None
        if selected.current_epoch_crosswalk is None
        else _independent_crosswalk_display_by_epoch(selected.current_epoch_crosswalk)
    )
    output_rows: list[dict[str, object]] = []
    for row in mapped:
        if row.get("account_id") not in providers:
            _fail()
        output_row = {
            "account_id": row["account_id"],
            "provider": providers[row["account_id"]],
            "strategy_id": row["strategy_id"],
            "effective_start": row["effective_start"],
            "effective_end": row["effective_end"],
            "admission_status": "BLOCKED",
            "admission_policy_reference": "NONE",
            "admission_authority_kind": "NONE",
            "funded_nav_record_ids": [],
            "continuity_to_successor": False,
            "status": "BLOCKED_ADMISSION",
            "blocking_reason": "FLOW_COMPLETENESS_NOT_PROVEN",
        }
        if display_by_epoch is not None:
            output_row.update(
                display_by_epoch[
                    tuple(
                        row[field]
                        for field in (
                            "account_id",
                            "strategy_id",
                            "effective_start",
                            "effective_end",
                        )
                    )
                ]
            )
        if _OUTPUT_FORBIDDEN_NUMERIC_FIELDS.intersection(output_row):
            _fail()
        output_rows.append(output_row)
    expected = {
        "schema_version": 8,
        "artifact": "account-strategy-manifest",
        "accounting_currency": _normalize_provider_navs(selected)[1],
        "authority_readiness": {
            "authority_status": "SOURCE_BOUND_CANDIDATE",
            "independent_authority_verification": "NOT_PERFORMED",
            "publication_readiness": "NOT_READY",
        },
        "source_identities": {
            "ledger": ledger_source,
            "baseline": baseline_source,
            "classified_flows": {
                "artifact": classified_artifact,
                "schema_version": 4,
                "ledger_identity": ledger_source,
                "classification_policy": {
                    "policy_id": policy_payload.get("policy_id"),
                    "policy_version": policy_payload.get("policy_version"),
                    "policy_as_of_date": policy_payload.get("as_of_date"),
                    "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
                },
            },
            "flow_classification_policy": policy_identity,
            "flow_coverage": {
                "artifact": coverage_artifact,
                "ledger_identity": ledger_source,
                "classified_flows_artifact": classified_artifact,
            },
            "admission_policy": {
                "policy_id": "MAPPED_EPOCH_ADMISSION_V1",
                "policy_version": "1",
                "artifact": admission_artifact,
                "assignment": b1_state["mapped_assignments_identity"],
            },
            "assignment": b1_state["mapped_assignments_identity"],
            "calculation_code": selected_identity["code_sources"]["epoch_producer"],
            "selected_inputs": selected_artifact,
            "account_assignment_perimeter": perimeter_artifact,
            "calculation_inputs": calculation_artifact,
            "code_identities": code_artifact,
        },
        "epoch_continuity": {
            "rule_id": "EXPLICIT_FUNDED_EPOCH_CONTINUITY_V1",
            "status": "POLICY_BOUND_ONLY",
            "rule": "Blocked candidates do not establish funded continuity.",
        },
        "flow_reconciliation": {
            "status": "ONE_TO_ONE_VERIFIED",
            "method": "EXACT_RAW_RECORD_TO_CLASSIFIED_EVENT_BINDING",
        },
        "rows": output_rows,
    }
    if (
        _strict_object(subject_payloads["outputs/account-strategy-manifest.json"])
        != expected
    ):
        _fail()


def _derive_independent_population_counts(
    selected: IndependentVerifierInputs,
    subject_payloads: Mapping[str, bytes],
) -> dict[str, int]:
    """Derive report population arithmetic from selected and subject bytes."""

    ledger = _ledger_with_derived(selected)
    navs = ledger.get("nav_observations")
    flows = ledger.get("flows")
    classified = _strict_object(subject_payloads["authority/classified-flows.json"])
    perimeter = _strict_object(
        subject_payloads["authority/account-assignment-perimeter.json"]
    )
    events = classified.get("events")
    unassigned_facts = perimeter.get("unassigned_facts")
    if not all(
        isinstance(rows, list) for rows in (navs, flows, events, unassigned_facts)
    ):
        _fail()
    classified_count = 0
    blocked_count = 0
    for event in events:
        if not isinstance(event, dict):
            _fail()
        status = event.get("classification_status")
        if status == "CLASSIFIED":
            classified_count += 1
        elif status == "BLOCKED":
            blocked_count += 1
        else:
            _fail()
    unmatched_nav_count = 0
    unmatched_raw_flow_count = 0
    for fact in unassigned_facts:
        if not isinstance(fact, dict):
            _fail()
        source_kind = fact.get("source_kind")
        if source_kind == "NAV":
            unmatched_nav_count += 1
        elif source_kind == "RAW_FLOW":
            unmatched_raw_flow_count += 1
        else:
            _fail()
    unmatched_fact_count = unmatched_nav_count + unmatched_raw_flow_count
    unmatched_projection_count = unmatched_nav_count + 2 * unmatched_raw_flow_count
    if (
        perimeter.get("unassigned_fact_count") != unmatched_fact_count
        or perimeter.get("unassigned_projection_count") != unmatched_projection_count
    ):
        _fail()
    return {
        "observed_nav_fact_count": len(navs),
        "observed_raw_flow_count": len(flows),
        "classified_event_count": len(events),
        "classified_event_classified_count": classified_count,
        "classified_event_blocked_count": blocked_count,
        "unmatched_nav_fact_count": unmatched_nav_count,
        "unmatched_raw_flow_fact_count": unmatched_raw_flow_count,
        "unmatched_fact_count": unmatched_fact_count,
        "unmatched_projection_count": unmatched_projection_count,
    }


def _derive_c4b3_2b2_fragments(
    selected: IndependentVerifierInputs,
    subject_payloads: Mapping[str, bytes],
    b1_state: Mapping[str, object],
) -> dict[str, object]:
    baseline = _strict_object(selected.baseline.contents)
    ledger = _strict_object(selected.provider_ledger.contents)
    accounts = baseline.get("accounts")
    assignments = baseline.get("assignments")
    navs = ledger.get("nav_observations")
    flows = ledger.get("flows")
    if not isinstance(accounts, list) or not isinstance(assignments, list):
        _fail()
    if not isinstance(navs, list) or not isinstance(flows, list):
        _fail()
    disposition_counts = {
        disposition: sum(
            isinstance(row, dict)
            and row.get("strategy_mapping_disposition") == disposition
            for row in assignments
        )
        for disposition in (
            "STRATEGY_MAPPED",
            "HISTORICAL_POOL_ONLY",
            "RESERVE_NOT_APPLICABLE",
        )
    }
    baseline_account_ids = {
        _internal_id(account.get("account_id"))
        for account in accounts
        if isinstance(account, dict)
    }
    assignment_account_ids = {
        _internal_id(assignment.get("account_id"))
        for assignment in assignments
        if isinstance(assignment, dict)
    }
    if not assignment_account_ids <= baseline_account_ids or len(assignments) != sum(
        disposition_counts.values()
    ):
        _fail()
    calc = _strict_object(subject_payloads["authority/calculation-inputs.json"])
    coverage = _strict_object(subject_payloads["authority/account-flow-coverage.json"])
    admission = _strict_object(
        subject_payloads["authority/mapped-epoch-admission.json"]
    )
    output = _strict_object(subject_payloads["outputs/account-strategy-manifest.json"])
    calc_rows = calc.get("rows")
    coverage_rows = coverage.get("rows")
    admission_rows = admission.get("rows")
    output_rows = output.get("rows")
    if not all(
        isinstance(rows, list)
        for rows in (calc_rows, coverage_rows, admission_rows, output_rows)
    ):
        _fail()
    perimeter = _strict_object(
        subject_payloads["authority/account-assignment-perimeter.json"]
    )
    mapped = _selected_current_epoch_rows(selected, perimeter["rows"])
    if not all(
        len(rows) == len(mapped)
        for rows in (calc_rows, coverage_rows, admission_rows, output_rows)
    ):
        _fail()

    def assignment_key(row: object) -> tuple[object, ...]:
        if not isinstance(row, dict):
            _fail()
        assignment = row.get("assignment", row.get("epoch", row))
        if not isinstance(assignment, dict) or not {
            "account_id",
            "strategy_id",
            "effective_start",
            "effective_end",
        } <= set(assignment):
            _fail()
        return tuple(
            assignment[key]
            for key in ("account_id", "strategy_id", "effective_start", "effective_end")
        )

    mapped_keys = {
        tuple(
            row[key]
            for key in ("account_id", "strategy_id", "effective_start", "effective_end")
        )
        for row in mapped
    }
    calc_keys = {assignment_key(row) for row in calc_rows}
    coverage_by_key = {assignment_key(row): row for row in coverage_rows}
    admission_by_key = {assignment_key(row): row for row in admission_rows}
    output_by_key = {assignment_key(row): row for row in output_rows}
    if (
        len(calc_keys) != len(mapped_keys)
        or len(coverage_by_key) != len(mapped_keys)
        or len(admission_by_key) != len(mapped_keys)
        or len(output_by_key) != len(mapped_keys)
        or calc_keys != mapped_keys
        or set(coverage_by_key) != mapped_keys
        or set(admission_by_key) != mapped_keys
        or set(output_by_key) != mapped_keys
    ):
        _fail()
    blocker_chains: list[dict[str, object]] = []
    for row in mapped:
        key = tuple(
            row[field]
            for field in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        )
        coverage_row = coverage_by_key[key]
        admission_row = admission_by_key[key]
        output_row = output_by_key[key]
        if (
            coverage_row.get("status") != "BLOCKED"
            or coverage_row.get("blocker_code") != "NO_ADMISSIBLE_V2_RECEIPT"
            or admission_row.get("status") != "BLOCKED"
            or admission_row.get("blocker_code") != "FLOW_COMPLETENESS_NOT_PROVEN"
            or output_row.get("status") != "BLOCKED_ADMISSION"
            or output_row.get("blocking_reason") != "FLOW_COMPLETENESS_NOT_PROVEN"
            or output_row.get("numeric_candidate") is not None
        ):
            _fail()
        blocker_chains.append(
            {
                "assignment": {
                    field: row[field]
                    for field in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                },
                "coverage": {
                    "status": "BLOCKED",
                    "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
                },
                "admission": {
                    "status": "BLOCKED",
                    "blocker_code": "FLOW_COMPLETENESS_NOT_PROVEN",
                },
                "output": {
                    "status": "BLOCKED_ADMISSION",
                    "blocking_reason": "FLOW_COMPLETENESS_NOT_PROVEN",
                },
                "numeric_candidate": False,
            }
        )
    counts = {
        "selected_baseline_account_count": len(baseline_account_ids),
        "account_count": len(assignment_account_ids),
        "assignment_count": len(assignments),
        "mapped_assignment_count": disposition_counts["STRATEGY_MAPPED"],
        "historical_assignment_count": disposition_counts["HISTORICAL_POOL_ONLY"],
        "reserve_assignment_count": disposition_counts["RESERVE_NOT_APPLICABLE"],
        **_derive_independent_population_counts(selected, subject_payloads),
        "calculation_input_row_count": len(calc_rows),
        "coverage_row_count": len(coverage_rows),
        "admission_row_count": len(admission_rows),
        "output_row_count": len(output_rows),
        "blocked_coverage_row_count": sum(
            row.get("status") == "BLOCKED" for row in coverage_rows
        ),
        "blocked_admission_row_count": sum(
            row.get("status") == "BLOCKED" for row in admission_rows
        ),
        "blocked_output_row_count": sum(
            row.get("status") == "BLOCKED_ADMISSION" for row in output_rows
        ),
    }
    return {
        "counts": counts,
        "blocker_chains": blocker_chains,
        "numeric_verification": {
            "candidate_count": 0,
            "comparison_count": 0,
            "discrepancy_count": 0,
            "disposition": "NOT_APPLICABLE_NO_CANDIDATES",
        },
        "invariants": {
            "exact_subject_set": True,
            "canonical_subject_bytes": True,
            "selected_source_binding": True,
            "fact_disposition_complete": True,
            "unassigned_numeric_exclusion": True,
            "classified_flow_reconciliation": True,
            "mapped_row_bijection": True,
            "blocker_propagation": True,
            "blocked_numeric_exclusion": True,
            "privacy_safe_projection": True,
        },
    }


def _assignment_record(row: Mapping[str, object]) -> dict[str, object]:
    return {
        key: row[key]
        for key in ("account_id", "strategy_id", "effective_start", "effective_end")
    }


def _sorted_mixed_output_assignments(
    rows: object,
) -> list[dict[str, object]]:
    """Reproduce generic manifest assembly: account order, then chain start."""

    if not isinstance(rows, list):
        _fail()
    ordered: list[dict[str, object]] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("account_id"), str)
            or not isinstance(row.get("effective_start"), str)
        ):
            _fail()
        ordered.append(row)
    ordered.sort(key=lambda row: (str(row["account_id"]), str(row["effective_start"])))
    return ordered


def _parent_receipt_route(
    derived: Mapping[str, object], routes: list[Mapping[str, object]]
) -> Mapping[str, object]:
    """The one selected receipt of the parent covering a derived epoch."""

    found = [
        route
        for route in routes
        if _sleeve_obj(route["receipt"]).get("account_id")
        == derived["parent_account_id"]
        and str(route["receipt"]["requested_start"]) <= str(derived["requested_start"])
        and str(route["receipt"]["requested_end"]) >= str(derived["requested_end"])
    ]
    if len(found) != 1:
        _fail()
    return found[0]


def _independent_calculation_routes(
    selected: IndependentVerifierInputs,
    mapped: list[dict[str, object]],
    routes: list[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    """Receipt-routed calculable epochs plus the derived sleeve epochs."""

    result = [route for route in routes if route["selected_assignment"] is not None]
    view = _independent_sleeve_view(selected)
    if view is None:
        return result
    by_key = {tuple(_assignment_record(row).values()): row for row in mapped}
    for derived in view.coverage:
        assignment = by_key.get(tuple(_sleeve_obj(derived["assignment"]).values()))
        if assignment is None:
            continue
        covering = _parent_receipt_route(derived, routes)
        result.append(
            {
                "receipt_id": covering["receipt_id"],
                "receipt": {
                    "requested_start": derived["requested_start"],
                    "requested_end": derived["requested_end"],
                },
                "selected_assignment": assignment,
            }
        )
    return result


def _mixed_coverage_payload(
    *,
    selected: IndependentVerifierInputs,
    mapped: list[dict[str, object]],
    ledger_identity: Mapping[str, object],
    classified_artifact: Mapping[str, object],
    mapped_identity: Mapping[str, object],
    routes: list[Mapping[str, object]],
) -> dict[str, object]:
    provider_ledger_date = _selected_date(selected.provider_ledger).isoformat()
    route_by_assignment = {
        tuple(_assignment_record(route["selected_assignment"]).values()): route
        for route in routes
        if route["selected_assignment"] is not None
    }
    view = _independent_sleeve_view(selected)
    derived_by_key = {
        tuple(_sleeve_obj(item["assignment"]).values()): item
        for item in ([] if view is None else view.coverage)
    }
    rows: list[dict[str, object]] = []
    for assignment in mapped:
        derived = derived_by_key.get(tuple(_assignment_record(assignment).values()))
        if derived is not None:
            covering = _parent_receipt_route(derived, routes)
            rows.append(
                {
                    "assignment": _assignment_record(assignment),
                    "requested_start": derived["requested_start"],
                    "requested_end": derived["requested_end"],
                    "status": "COMPLETE",
                    "authority_kind": _DERIVED_COVERAGE_KIND,
                    "receipt_artifact_id": covering["receipt_id"],
                    "blocker_code": None,
                    "blocker_detail": None,
                    "derivation": {
                        "parent_account_id": derived["parent_account_id"],
                        "sleeve_facts_sha256": derived["sleeve_facts_sha256"],
                    },
                }
            )
            continue
        route = route_by_assignment.get(tuple(_assignment_record(assignment).values()))
        complete = route is not None
        rows.append(
            {
                "assignment": _assignment_record(assignment),
                "requested_start": (
                    route["receipt"]["requested_start"]
                    if complete
                    else assignment["effective_start"]
                ),
                "requested_end": (
                    route["receipt"]["requested_end"]
                    if complete
                    else assignment["effective_end"] or provider_ledger_date
                ),
                "status": "COMPLETE" if complete else "BLOCKED",
                "authority_kind": (
                    "STATIC_SNAPSHOT_COVERAGE_V1"
                    if complete
                    and route["receipt"].get("coverage_mode")
                    == "STATIC_SNAPSHOT_COVERAGE_V1"
                    else "ACCOUNT_INTERVAL_RETRIEVAL_RECEIPT_V2"
                    if complete
                    else "NONE"
                ),
                "receipt_artifact_id": route["receipt_id"] if complete else None,
                "blocker_code": None if complete else "NO_ADMISSIBLE_V2_RECEIPT",
                "blocker_detail": (
                    None if complete else "No admissible retained receipt issuer."
                ),
            }
        )
    return {
        "schema_version": 2,
        "artifact_id": "authority/account-flow-coverage.json",
        "as_of_date": provider_ledger_date,
        "ledger_identity": dict(ledger_identity),
        "classified_flows_artifact": dict(classified_artifact),
        "mapped_assignments_identity": dict(mapped_identity),
        "receipt_artifacts": [
            {
                "artifact_id": route["receipt_id"],
                "sha256": route["receipt_snapshot"].sha256,
                "size_bytes": route["receipt_snapshot"].size_bytes,
            }
            for route in routes
        ],
        "rows": rows,
    }


def _validate_source_artifact_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "artifact_id",
        "as_of_date",
        "schema",
        "sha256",
        "size_bytes",
    }:
        _fail()
    _portable_identifier(value.get("artifact_id"))
    _date_value(value.get("as_of_date"))
    schema = value.get("schema")
    if (
        not isinstance(schema, dict)
        or set(schema) != {"name", "version"}
        or not isinstance(schema.get("name"), str)
        or not schema["name"]
        or not isinstance(schema.get("version"), str)
        or not schema["version"]
        or type(value.get("sha256")) is not str
        or _SHA256.fullmatch(value["sha256"]) is None
        or type(value.get("size_bytes")) is not int
        or value["size_bytes"] < 0
    ):
        _fail()
    return dict(value)


def _validate_provider_receipt_date_authority(
    selected: IndependentVerifierInputs,
    receipt: Mapping[str, object],
    manifest: Mapping[str, object],
    raw_artifacts: list[Mapping[str, object]],
) -> None:
    ledger_as_of_date = _selected_date(selected.provider_ledger)
    _date_value(manifest.get("as_of_date"))
    for artifact in raw_artifacts:
        _date_value(artifact.get("as_of_date"))
    for field_name in (
        "requested_start",
        "requested_end",
        "covered_start",
        "covered_end",
    ):
        _reject_provider_fact_after_ledger_date(
            _date_value(receipt.get(field_name)), ledger_as_of_date
        )


def _independent_tail_keys(
    selected: IndependentVerifierInputs,
) -> set[tuple[str, str, str, str | None]]:
    """Final closed epochs whose admission declares a funded end (#557).

    Re-derived from the baseline and the admission policy alone: the epoch ends
    on its crosswalk date, its evidence window runs through the cutoff.
    """

    baseline = _strict_object(_require_snapshot(selected.baseline).contents)
    policy = _strict_object(_require_snapshot(selected.admission_policy).contents)
    assignments = baseline.get("assignments")
    admissions = policy.get("admissions")
    if not isinstance(assignments, list) or not isinstance(admissions, list):
        _fail()
    declared = {
        (
            str(row["account_id"]),
            str(row["strategy_id"]),
            str(row["effective_start"]),
            str(row["effective_end"]),
        )
        for row in admissions
        if isinstance(row, dict)
        and row.get("status") == "FUNDED"
        and row.get("funded_end") is not None
        and row.get("effective_end") is not None
    }
    last: dict[str, dict[str, object]] = {}
    for assignment in assignments:
        if not isinstance(assignment, dict):
            _fail()
        account = str(assignment.get("account_id"))
        start = str(assignment.get("effective_start") or "")
        if account not in last or start > str(
            last[account].get("effective_start") or ""
        ):
            last[account] = assignment
    tails: set[tuple[str, str, str, str | None]] = set()
    for assignment in last.values():
        if (
            assignment.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
            and assignment.get("effective_end") is not None
        ):
            key = (
                str(assignment["account_id"]),
                str(assignment["strategy_id"]),
                str(assignment["effective_start"]),
                str(assignment["effective_end"]),
            )
            if key in declared:
                tails.add(key)
    return {(a, s, st, e) for a, s, st, e in tails}


def _is_tail(
    assignment: Mapping[str, object],
    tail_keys: set[tuple[str, str, str, str | None]],
) -> bool:
    end = assignment.get("effective_end")
    return (
        end is not None
        and (
            str(assignment["account_id"]),
            str(assignment["strategy_id"]),
            str(assignment["effective_start"]),
            str(end),
        )
        in tail_keys
    )


def _verify_mixed_authority_and_coverage_many(
    selected: IndependentVerifierInputs,
    normalized: Mapping[str, bytes],
    mapped: list[dict[str, object]],
    providers: Mapping[str, str],
    mapped_identity: Mapping[str, object],
) -> dict[str, object]:
    """Independently route every selected receipt and reconstruct coverage."""

    tail_keys = _independent_tail_keys(selected)
    sleeve_view = _independent_sleeve_view(selected)
    residual_parents = frozenset() if sleeve_view is None else sleeve_view.residual
    authority_snapshot = _require_snapshot(selected.receipt_authority)
    if normalized[_MIXED_AUTHORITY_PATH] != authority_snapshot.contents:
        _fail()
    authority = _strict_object(authority_snapshot.contents)
    entries = authority.get("receipts")
    receipt_ids = sorted(selected.retrieval_receipts, key=str.casefold)
    if (
        set(authority) != {"schema_version", "artifact_id", "as_of_date", "receipts"}
        or authority.get("schema_version") != 1
        or authority.get("artifact_id") != _MIXED_AUTHORITY_PATH
        or not isinstance(entries, list)
        or [
            entry.get("artifact_id") if isinstance(entry, dict) else None
            for entry in entries
        ]
        != receipt_ids
    ):
        _fail()
    coverage_date = _selected_date(selected.provider_ledger)
    authority_date = _date_value(authority.get("as_of_date"))
    if authority_date > coverage_date:
        _fail()
    ledger = _strict_object(selected.provider_ledger.contents)
    generations = ledger.get("provider_generations")
    if not isinstance(generations, list):
        _fail()
    routes: list[dict[str, object]] = []
    routed_keys: set[tuple[object, ...]] = set()
    expected_entries: list[dict[str, object]] = []
    receipt_fields = {
        "schema_version",
        "artifact_id",
        "issuer",
        "provider",
        "provider_generation",
        "provider_generation_manifest",
        "raw_evidence_artifacts",
        "account_id",
        "source_account_identity_sha256",
        "requested_start",
        "requested_end",
        "covered_start",
        "covered_end",
        "required_categories",
        "completed_categories",
        "unavailable_categories",
        "pagination",
        "completeness",
        "truncated",
    }
    for receipt_id in receipt_ids:
        receipt_snapshot = selected.retrieval_receipts[receipt_id]
        if normalized.get(receipt_id) != receipt_snapshot.contents:
            _fail()
        receipt = _strict_object(receipt_snapshot.contents)
        if receipt.get("coverage_mode") == "STATIC_SNAPSHOT_COVERAGE_V1":
            static_fields = {
                "schema_version",
                "artifact_id",
                "coverage_mode",
                "issuer",
                "provider",
                "account_id",
                "strategy_id",
                "requested_start",
                "requested_end",
                "calculation_cutoff_date",
                "date_convention",
                "provider_generation",
                "staged_inputs",
                "observed_facts",
                "missing_intervals",
                "coverage_as_of_date",
                "completeness",
            }
            if (
                set(receipt) != static_fields
                or receipt.get("schema_version") != 3
                or receipt.get("artifact_id") != receipt_id
                or receipt.get("date_convention") != "EOD_PROVIDER_NAV_RECOGNITION_DATE"
                or receipt.get("completeness") != "RETAINED_STATIC_SNAPSHOT_ONLY"
            ):
                _fail()
            account_id = _internal_id(receipt.get("account_id"))
            strategy_id = _fact_text(receipt.get("strategy_id"))
            requested_start = _date_value(receipt.get("requested_start"))
            requested_end = _date_value(receipt.get("requested_end"))
            generation = receipt.get("provider_generation")
            staged = receipt.get("staged_inputs")
            facts = receipt.get("observed_facts")
            if (
                not isinstance(generation, dict)
                or not isinstance(staged, dict)
                or not isinstance(facts, dict)
                or set(facts) != {"nav", "flows", "capital_anchors"}
            ):
                _fail()
            provider = _fact_text(receipt.get("provider"))
            matching_generations = [
                row
                for row in generations
                if isinstance(row, dict)
                and row.get("provider_id") == provider.casefold()
                and providers.get(account_id) == provider
                and generation
                == {
                    "generation_id": row.get("generation_id"),
                    "catalog_sha256": row.get("catalog_sha256"),
                    "manifest_sha256": row.get("manifest_sha256"),
                    "generation_as_of_date": row.get("generation_as_of_date"),
                }
            ]
            if (
                len(matching_generations) != 1
                or selected.current_epoch_crosswalk is None
            ):
                _fail()
            selected_identity = _selected_identity(selected)
            expected_staged = {
                "provider_ledger_generation_id": selected_identity["provider_ledger"][
                    "generation_id"
                ],
                "provider_ledger_artifact": selected_identity["provider_ledger"][
                    "artifact"
                ],
                "baseline_sha256": selected.baseline.sha256,
                "baseline_size_bytes": selected.baseline.size_bytes,
                "crosswalk_sha256": selected.current_epoch_crosswalk.sha256,
                "crosswalk_size_bytes": selected.current_epoch_crosswalk.size_bytes,
            }
            cutoff = _current_epoch_calculation_cutoff(selected)
            if (
                staged != expected_staged
                or cutoff is None
                or receipt.get("calculation_cutoff_date") != cutoff.isoformat()
            ):
                _fail()
            if (
                requested_start > requested_end
                or requested_end > coverage_date
                or cutoff > coverage_date
                or _date_value(receipt.get("coverage_as_of_date")) != coverage_date
            ):
                _fail()

            def static_fact_date(row: Mapping[str, object], kind: str) -> str:
                return str(
                    row.get("as_of_date")
                    if kind == "nav"
                    else row.get("nav_recognition_date", row.get("event_date"))
                )

            for kind, field in (
                ("nav", "nav_observations"),
                ("flows", "flows"),
                ("capital_anchors", "capital_anchors"),
            ):
                source_rows = ledger.get(field)
                if not isinstance(source_rows, list):
                    _fail()
                rows = [
                    row
                    for row in source_rows
                    if isinstance(row, Mapping)
                    and row.get("account_id") == account_id
                    and (
                        kind != "capital_anchors"
                        or row.get("strategy_id") == strategy_id
                    )
                    and not (
                        kind == "flows"
                        and row.get("provider_account_scope") == "ACCOUNT_ONLY"
                        and row.get("mapping_status") == "UNMAPPED"
                        and "strategy_id" in row
                        and row["strategy_id"] is None
                    )
                    and requested_start.isoformat()
                    <= static_fact_date(row, kind)
                    <= requested_end.isoformat()
                ]
                dates = sorted(static_fact_date(row, kind) for row in rows)
                if facts.get(kind) != {
                    "row_count": len(rows),
                    "first_date": dates[0] if dates else None,
                    "last_date": dates[-1] if dates else None,
                }:
                    _fail()
            expected_missing: list[dict[str, str]] = []
            coverage_rows = ledger.get("provider_coverage")
            if isinstance(coverage_rows, list):
                for coverage_row in coverage_rows:
                    coverage = (
                        coverage_row.get("coverage")
                        if isinstance(coverage_row, Mapping)
                        else None
                    )
                    if (
                        not isinstance(coverage, Mapping)
                        or str(coverage_row.get("provider_id", "")).upper() != provider
                    ):
                        continue
                    for item in coverage.get("declared_exclusions", []):
                        exclusion_item = (
                            item.get("item") if isinstance(item, Mapping) else None
                        )
                        if isinstance(
                            exclusion_item, str
                        ) and exclusion_item.startswith("nav_missing_date:"):
                            missing = exclusion_item.removeprefix("nav_missing_date:")
                            if (
                                requested_start.isoformat()
                                <= missing
                                <= requested_end.isoformat()
                            ):
                                expected_missing.append(
                                    {
                                        "start": missing,
                                        "end": missing,
                                        "reason": "PROVIDER_DECLARED_NAV_MISSING",
                                    }
                                )
            if receipt.get("missing_intervals") != sorted(
                expected_missing, key=lambda item: item["start"]
            ):
                _fail()
            contract = {
                "coverage_mode": "STATIC_SNAPSHOT_COVERAGE_V1",
                "account_id": account_id,
                "strategy_id": strategy_id,
                "requested_start": requested_start.isoformat(),
                "requested_end": requested_end.isoformat(),
                "provider_generation": generation,
                "staged_inputs": staged,
            }
            expected_entries.append(
                {
                    "artifact_id": receipt_id,
                    "sha256": receipt_snapshot.sha256,
                    "size_bytes": receipt_snapshot.size_bytes,
                    "contract": contract,
                }
            )
            routed = [
                assignment
                for assignment in mapped
                if assignment["account_id"] == account_id
                and assignment["strategy_id"] == strategy_id
                and assignment["effective_start"] == requested_start.isoformat()
                and (
                    assignment["effective_end"] is None
                    or assignment["effective_end"] == requested_end.isoformat()
                )
            ]
            if not routed and account_id in residual_parents:
                routes.append(
                    {
                        "receipt_id": receipt_id,
                        "receipt_snapshot": receipt_snapshot,
                        "receipt": receipt,
                        "selected_assignment": None,
                    }
                )
                continue
            if len(routed) != 1:
                _fail()
            route_key = tuple(_assignment_record(routed[0]).values())
            if route_key in routed_keys:
                _fail()
            routed_keys.add(route_key)
            routes.append(
                {
                    "receipt_id": receipt_id,
                    "receipt_snapshot": receipt_snapshot,
                    "receipt": receipt,
                    "selected_assignment": routed[0],
                }
            )
            continue
        if (
            set(receipt) != receipt_fields
            or receipt.get("schema_version") != 2
            or receipt.get("artifact_id") != receipt_id
        ):
            _fail()
        issuer = receipt.get("issuer")
        pagination = receipt.get("pagination")
        if (
            not isinstance(issuer, dict)
            or set(issuer) != {"adapter_name", "adapter_version", "code_sha256"}
            or any(
                not isinstance(issuer.get(key), str) or not issuer[key]
                for key in ("adapter_name", "adapter_version")
            )
            or type(issuer.get("code_sha256")) is not str
            or _SHA256.fullmatch(issuer["code_sha256"]) is None
            or not isinstance(pagination, dict)
            or set(pagination)
            != {
                "mode",
                "page_count",
                "request_response_chain_sha256",
                "terminal_response_sha256",
                "terminal",
            }
            or not isinstance(pagination.get("mode"), str)
            or not pagination["mode"]
            or type(pagination.get("page_count")) is not int
            or pagination["page_count"] <= 0
            or type(pagination.get("request_response_chain_sha256")) is not str
            or _SHA256.fullmatch(pagination["request_response_chain_sha256"]) is None
            or type(pagination.get("terminal_response_sha256")) is not str
            or _SHA256.fullmatch(pagination["terminal_response_sha256"]) is None
            or pagination.get("terminal") is not True
            or receipt.get("completeness")
            != "EXACT_ACCOUNT_INTERVAL_ALL_APPLICABLE_CATEGORIES"
            or receipt.get("truncated") is not False
        ):
            _fail()
        _fact_text(issuer["adapter_name"])
        _fact_text(issuer["adapter_version"])
        _fact_text(pagination["mode"])
        manifest = _validate_source_artifact_object(
            receipt.get("provider_generation_manifest")
        )
        raw_artifacts = receipt.get("raw_evidence_artifacts")
        if not isinstance(raw_artifacts, list) or not raw_artifacts:
            _fail()
        normalized_raw = [
            _validate_source_artifact_object(value) for value in raw_artifacts
        ]
        _validate_provider_receipt_date_authority(
            selected, receipt, manifest, normalized_raw
        )
        if len({value["artifact_id"] for value in normalized_raw}) != len(
            normalized_raw
        ):
            _fail()
        categories: dict[str, list[str]] = {}
        for key in (
            "required_categories",
            "completed_categories",
            "unavailable_categories",
        ):
            value = receipt.get(key)
            if not isinstance(value, list):
                _fail()
            normalized_categories = [_fact_text(item) for item in value]
            if normalized_categories != sorted(set(normalized_categories)):
                _fail()
            categories[key] = normalized_categories
        provider = receipt.get("provider")
        if (
            not categories["required_categories"]
            or categories["required_categories"] != categories["completed_categories"]
            or categories["unavailable_categories"]
            or not isinstance(provider, str)
            or not provider
            or type(receipt.get("provider_generation")) is not str
            or _SHA256.fullmatch(receipt["provider_generation"]) is None
            or type(receipt.get("source_account_identity_sha256")) is not str
            or _SHA256.fullmatch(receipt["source_account_identity_sha256"]) is None
        ):
            _fail()
        account_id = _internal_id(receipt.get("account_id"))
        requested_start = _date_value(receipt.get("requested_start"))
        requested_end = _date_value(receipt.get("requested_end"))
        covered_start = _date_value(receipt.get("covered_start"))
        covered_end = _date_value(receipt.get("covered_end"))
        if (
            requested_start > requested_end
            or covered_start != requested_start
            or covered_end != requested_end
        ):
            _fail()
        contract = {
            "adapter_name": issuer["adapter_name"],
            "adapter_version": issuer["adapter_version"],
            "code_sha256": issuer["code_sha256"],
            "provider": provider,
            "account_id": account_id,
            "source_account_identity_sha256": receipt["source_account_identity_sha256"],
            "provider_generation": receipt["provider_generation"],
            "provider_generation_manifest": manifest,
            "raw_evidence_artifacts": normalized_raw,
            "required_categories": categories["required_categories"],
            "pagination": dict(pagination),
        }
        expected_entries.append(
            {
                "artifact_id": receipt_id,
                "sha256": receipt_snapshot.sha256,
                "size_bytes": receipt_snapshot.size_bytes,
                "contract": contract,
            }
        )
        if account_id not in providers:
            _fail()
        generation_matches = [
            row
            for row in generations
            if isinstance(row, dict)
            and isinstance(row.get("provider_id"), str)
            and _INTERNAL_ID.fullmatch(row["provider_id"]) is not None
            and providers[account_id] == row["provider_id"].upper()
            and provider == row["provider_id"].upper()
            and receipt["provider_generation"] == row.get("generation_id")
            and manifest["sha256"] == row.get("manifest_sha256")
            and manifest["as_of_date"] == row.get("generation_as_of_date")
        ]
        if len(generation_matches) != 1:
            _fail()
        routed = [
            assignment
            for assignment in mapped
            if assignment["account_id"] == account_id
            and assignment["effective_start"] == requested_start.isoformat()
            and (
                assignment["effective_end"] is None
                or assignment["effective_end"] == requested_end.isoformat()
                or (
                    requested_end == _selected_date(selected.provider_ledger)
                    and _is_tail(assignment, tail_keys)
                )
            )
        ]
        if not routed and account_id in residual_parents:
            routes.append(
                {
                    "receipt_id": receipt_id,
                    "receipt_snapshot": receipt_snapshot,
                    "receipt": receipt,
                    "selected_assignment": None,
                }
            )
            continue
        if len(routed) != 1:
            _fail()
        route_key = tuple(_assignment_record(routed[0]).values())
        if route_key in routed_keys:
            _fail()
        routed_keys.add(route_key)
        routes.append(
            {
                "receipt_id": receipt_id,
                "receipt_snapshot": receipt_snapshot,
                "receipt": receipt,
                "selected_assignment": routed[0],
            }
        )
    if entries != expected_entries:
        _fail()
    selected_identity = _selected_identity(selected)
    ledger_pair = {
        "generation_id": selected_identity["provider_ledger"]["generation_id"],
        "artifact": selected_identity["provider_ledger"]["artifact"],
    }
    classified_artifact = _subject_artifact(
        normalized["authority/classified-flows.json"],
        "authority/classified-flows.json",
        "classified-flows",
        "5",
        selected.generation_as_of_date.isoformat(),
    )
    coverage_expected = _mixed_coverage_payload(
        selected=selected,
        mapped=mapped,
        ledger_identity=ledger_pair,
        classified_artifact=classified_artifact,
        mapped_identity=mapped_identity,
        routes=routes,
    )
    if (
        _strict_object(normalized["authority/account-flow-coverage.json"])
        != coverage_expected
    ):
        _fail()
    return {
        "routes": routes,
        "authority": authority,
        "coverage": coverage_expected,
        "classified_artifact": classified_artifact,
        "ledger_pair": ledger_pair,
    }


def _retained_flow_scope_metadata(raw: Mapping[str, object]) -> dict[str, object]:
    """Independently validate provider mapping fields used for flow admission."""

    metadata: dict[str, object] = {}
    if "provider_account_scope" in raw:
        metadata["provider_account_scope"] = _fact_text(
            raw.get("provider_account_scope")
        )
    if "mapping_status" in raw:
        mapping_status = raw.get("mapping_status")
        if mapping_status not in {"MAPPED", "UNMAPPED"}:
            _fail()
        metadata["mapping_status"] = mapping_status
    if "strategy_id" in raw:
        metadata["strategy_id"] = _internal_id(raw.get("strategy_id"), nullable=True)
    return metadata


@dataclass(frozen=True)
class _SleeveView:
    """What the selected sleeve-facts artifacts imply, derived independently.

    This is the verifier's own reading of the artifacts; it shares no code with
    the epoch producer or the generation builder.  See the producer's
    "Derived nested-strategy sleeves" header for the conventions it must agree
    with.
    """

    accounts: tuple[dict[str, object], ...]
    assignments: tuple[dict[str, object], ...]
    residual: frozenset[str]
    sleeve_accounts: frozenset[str]
    perimeter_accounts: frozenset[str]
    navs: tuple[dict[str, object], ...]
    flows: tuple[dict[str, object], ...]
    references: tuple[dict[str, object], ...]
    admissions: tuple[dict[str, object], ...]
    coverage: tuple[dict[str, object], ...]
    reconciliations: tuple[dict[str, object], ...]


_SLEEVE_ROLES: Final[Mapping[str, str]] = MappingProxyType(
    {"sleeve-account-facts": "RESIDUAL", "quantbase-fund-facts": "LIVE"}
)
_SLEEVE_REQUIRED_KEYS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "sleeve-account-facts": frozenset(
            "schema_version artifact_id parent_account_id provider_id base_currency "
            "window bindings closure_policy sleeves perimeter_account flows "
            "allocated_cost closure disclosures".split()
        ),
        "quantbase-fund-facts": frozenset(
            "schema_version artifact_id parent_account_id provider_id base_currency "
            "data_cutoff window bindings conventions sleeves perimeter_account flows "
            "closure open_items disclosures".split()
        ),
    }
)
_SLEEVE_GENERATION_ID: Final = re.compile(r"^[A-Za-z0-9._:@+-]{1,200}$")
_SLEEVE_FLOW_TYPE: Final[str] = "TRANSFER:INTERNAL:SLEEVE"
_DERIVED_COVERAGE_KIND: Final[str] = "DERIVED_SLEEVE_COVERAGE_V1"


def _sleeve_obj(value: object) -> dict[str, object]:
    if type(value) is not dict:
        _fail()
    return value


def _sleeve_seq(value: object) -> list[object]:
    if type(value) is not list:
        _fail()
    return value


def _sleeve_money(value: object) -> Decimal:
    if type(value) is not str or _CANONICAL_NUMERIC.fullmatch(value) is None:
        _fail()
    return _parse_retained_source_decimal(value)


def _sleeve_contract_holds(payload: Mapping[str, object], provider: str) -> bool:
    """Governed top-level keys, provider and every hash binding are present."""

    if not _SLEEVE_REQUIRED_KEYS[str(payload["artifact_id"])] <= set(payload):
        return False
    if str(payload.get("provider_id")).casefold() != provider.casefold():
        return False
    bindings = payload.get("bindings")
    if not isinstance(bindings, Mapping):
        return False

    def is_sha(value: object) -> bool:
        return isinstance(value, str) and _SHA256.fullmatch(value) is not None

    if not (
        is_sha(bindings.get("tag_map_sha256"))
        and is_sha(bindings.get("declared_inputs_sha256"))
    ):
        return False
    generation = bindings.get("parent_ledger_generation_id")
    if (
        not isinstance(generation, str)
        or _SLEEVE_GENERATION_ID.fullmatch(generation) is None
    ):
        return False
    if payload["artifact_id"] == "sleeve-account-facts":
        if not is_sha(bindings.get("account_rows_sha256")):
            return False
    else:
        source = bindings.get("source_sha256")
        if not isinstance(source, Mapping):
            return False
        dashboards = source.get("dashboards")
        if (
            not isinstance(dashboards, Mapping)
            or not dashboards
            or not is_sha(source.get("account_activity"))
            or not is_sha(source.get("accounts"))
            or not all(is_sha(item) for item in dashboards.values())
        ):
            return False
    code = bindings.get("builder_code")
    if not isinstance(code, Mapping):
        return False
    files = code.get("files")
    return (
        isinstance(files, Mapping)
        and bool(files)
        and is_sha(code.get("sha256"))
        and all(is_sha(item) for item in files.values())
    )


def _bound_parent_generation(payload: Mapping[str, object]) -> str | None:
    bindings = payload.get("bindings")
    if not isinstance(bindings, Mapping):
        return None
    value = bindings.get("parent_ledger_generation_id")
    return value if isinstance(value, str) else None


def _ledger_provider_generations(ledger: InputSnapshot) -> dict[str, str]:
    """Each provider's source generation id as the selected provider ledger names it.

    A sleeve-facts artifact is bound to the generation its rows were derived
    from; it must be the very generation the selected provider ledger admits.
    """

    rows = _strict_object(ledger.contents).get("provider_generations")
    if not isinstance(rows, list):
        _fail()
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail()
        provider = row.get("provider_id")
        generation = row.get("generation_id")
        if (
            not isinstance(provider, str)
            or not isinstance(generation, str)
            or not provider
            or provider.casefold() in result
        ):
            _fail()
        result[provider.casefold()] = generation
    return result


def _independent_sleeve_view(selected: IndependentVerifierInputs) -> _SleeveView | None:
    """Re-derive every derived account, epoch, fact and coverage row."""

    if not selected.sleeve_facts:
        return None
    baseline = _strict_object(selected.baseline.contents)
    providers = _baseline_account_providers(baseline.get("accounts"))
    ledger_generations = _ledger_provider_generations(selected.provider_ledger)
    accounts: list[dict[str, object]] = []
    assignments: list[dict[str, object]] = []
    navs: list[dict[str, object]] = []
    flows: list[dict[str, object]] = []
    references: list[dict[str, object]] = []
    admissions: list[dict[str, object]] = []
    coverage: list[dict[str, object]] = []
    reconciliations: list[dict[str, object]] = []
    residual: set[str] = set()
    sleeve_accounts: set[str] = set()
    perimeter_accounts: set[str] = set()
    used_accounts: set[str] = set()
    used_parents: set[str] = set()
    used_digests: set[str] = set()
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        for snapshot in selected.sleeve_facts:
            if snapshot.sha256 in used_digests:
                _fail()
            used_digests.add(snapshot.sha256)
            payload = _strict_object(snapshot.contents)
            role = _SLEEVE_ROLES.get(str(payload.get("artifact_id")))
            if role is None or payload.get("schema_version") != 1:
                _fail()
            parent = _internal_id(payload.get("parent_account_id"))
            if parent is None or parent in used_parents or parent not in providers:
                _fail()
            if not _sleeve_contract_holds(payload, providers[parent]):
                _fail()
            if _bound_parent_generation(payload) != ledger_generations.get(
                providers[parent].casefold()
            ):
                _fail()
            used_parents.add(parent)
            if payload.get("base_currency") != "USD":
                _fail()
            window = _sleeve_obj(payload.get("window"))
            window_first = _date_value(window.get("first_day")).isoformat()
            window_last = _date_value(window.get("last_day")).isoformat()
            if window_first > window_last:
                _fail()
            perimeter = _sleeve_obj(payload.get("perimeter_account"))
            pool = _internal_id(perimeter.get("account_id"))
            if pool != f"{parent}-unallocated" or pool in used_accounts:
                _fail()
            used_accounts.add(pool)
            perimeter_accounts.add(pool)
            tag = snapshot.sha256[:16]
            if role == "RESIDUAL":
                residual.add(parent)
            accounts.append(
                {
                    "account_id": pool,
                    "base_currency": "USD",
                    "provider": providers[parent],
                    "role": "INVESTMENT",
                }
            )
            assignments.append(
                {
                    "assignment_id": f"sleeve-assignment:{tag}:{pool}",
                    "account_id": pool,
                    "strategy_id": None,
                    "strategy_mapping_disposition": "HISTORICAL_POOL_ONLY",
                    "effective_start": window_first,
                    "effective_end": None,
                }
            )
            value_key = "nav_end_after_alloc" if role == "RESIDUAL" else "nav_end"
            mine: set[str] = set()
            nav_totals: dict[str, Decimal] = {}
            extra_legs: list[dict[str, object]] = []
            for raw_sleeve in _sleeve_seq(payload.get("sleeves")):
                sleeve = _sleeve_obj(raw_sleeve)
                sleeve_id = sleeve.get("sleeve_id")
                account = _internal_id(sleeve.get("account_id"))
                strategy = _internal_id(sleeve.get("strategy_id"))
                if (
                    type(sleeve_id) is not str
                    or not sleeve_id
                    or account != f"{parent}-{sleeve_id}"
                    or account in used_accounts
                    or strategy is None
                ):
                    _fail()
                used_accounts.add(account)
                mine.add(account)
                sleeve_accounts.add(account)
                days = _sleeve_seq(sleeve.get("days"))
                if not days:
                    _fail()
                previous = ""
                carried = Decimal(0)
                exit_day: str | None = None
                values: dict[str, Decimal] = {}
                for position, raw_day in enumerate(days):
                    row = _sleeve_obj(raw_day)
                    day = _date_value(row.get("day")).isoformat()
                    if day <= previous:
                        _fail()
                    previous = day
                    value = _sleeve_money(row.get(value_key))
                    if role == "RESIDUAL":
                        carried += _sleeve_money(row.get("alloc_total"))
                    if value < 0:
                        _fail()
                    values[day] = value
                    if role == "RESIDUAL" and row.get("event") == "EXIT":
                        if position != len(days) - 1 or value != 0:
                            _fail()
                        exit_day = day
                    navs.append(
                        {
                            "record_id": f"sleeve-nav:{tag}:{account}:{day}",
                            "account_id": account,
                            "as_of_date": day,
                            "value_basis": "STRATEGY_NAV",
                            "nav": {
                                "currency": "USD",
                                "status": "AVAILABLE",
                                "value": _format_decimal(value),
                            },
                        }
                    )
                first_day = _date_value(sleeve.get("first_day")).isoformat()
                last_day = _date_value(sleeve.get("last_day")).isoformat()
                if (
                    _sleeve_obj(days[0]).get("day") != first_day
                    or _sleeve_obj(days[-1]).get("day") != last_day
                    or first_day < window_first
                    or last_day > window_last
                    or values[first_day] <= 0
                ):
                    _fail()
                if role == "LIVE":
                    if values[last_day] != 0:
                        _fail()
                    end: str | None = last_day
                else:
                    end = exit_day
                for day, value in values.items():
                    nav_totals[day] = nav_totals.get(day, Decimal(0)) + value
                assignments.append(
                    {
                        "assignment_id": f"sleeve-assignment:{tag}:{account}",
                        "account_id": account,
                        "strategy_id": strategy,
                        "strategy_mapping_disposition": "STRATEGY_MAPPED",
                        "effective_start": first_day,
                        "effective_end": end,
                    }
                )
                accounts.append(
                    {
                        "account_id": account,
                        "base_currency": "USD",
                        "provider": providers[parent],
                        "role": "INVESTMENT",
                    }
                )
                epoch = {
                    "account_id": account,
                    "strategy_id": strategy,
                    "effective_start": first_day,
                    "effective_end": end,
                }
                admissions.append(
                    {
                        **epoch,
                        "status": "FUNDED",
                        "continuity_to_successor": False,
                        "authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                        "funded_nav_record_ids": [
                            f"sleeve-nav:{tag}:{account}:{first_day}"
                        ],
                    }
                )
                coverage.append(
                    {
                        "assignment": epoch,
                        "requested_start": first_day,
                        "requested_end": end or window_last,
                        "parent_account_id": parent,
                        "sleeve_facts_sha256": snapshot.sha256,
                    }
                )
                if exit_day is not None and carried != 0:
                    pair = f"{exit_day}:settle-{sleeve_id}"
                    for leg_account, leg_class in (
                        (account, "INTERNAL_OUT" if carried > 0 else "INTERNAL_IN"),
                        (pool, "INTERNAL_IN" if carried > 0 else "INTERNAL_OUT"),
                    ):
                        extra_legs.append(
                            {
                                "pair_id": pair,
                                "account_id": leg_account,
                                "class": leg_class,
                                "event_date": exit_day,
                                "nav_recognition_date": exit_day,
                                "amount": _format_decimal(abs(carried)),
                            }
                        )
            if not mine:
                _fail()
            totals: dict[str, Decimal] = {}
            sizes: dict[str, int] = {}
            for raw_leg in (*_sleeve_seq(payload.get("flows")), *extra_legs):
                leg = _sleeve_obj(raw_leg)
                account = _internal_id(leg.get("account_id"))
                flow_class = leg.get("class")
                if flow_class not in {"INTERNAL_IN", "INTERNAL_OUT"}:
                    _fail()
                if leg.get("flow_type", _SLEEVE_FLOW_TYPE) != _SLEEVE_FLOW_TYPE:
                    _fail()
                referenced = leg.get("leg_source") == "PARENT_LEDGER_REFERENCE"
                if account not in mine and account != pool and not referenced:
                    _fail()
                amount = _sleeve_money(leg.get("amount"))
                event_day = _date_value(leg.get("event_date")).isoformat()
                recognition = _date_value(leg.get("nav_recognition_date")).isoformat()
                pair_id = leg.get("pair_id")
                if amount <= 0 or recognition < event_day or type(pair_id) is not str:
                    _fail()
                if not pair_id:
                    _fail()
                signed = amount if flow_class == "INTERNAL_IN" else -amount
                totals[pair_id] = totals.get(pair_id, Decimal(0)) + signed
                sizes[pair_id] = sizes.get(pair_id, 0) + 1
                if referenced:
                    if role != "LIVE":
                        _fail()
                    references.append(
                        {
                            "pair_id": pair_id,
                            "account_id": account,
                            "event_date": event_day,
                            "class": flow_class,
                            "amount": _format_decimal(amount),
                        }
                    )
                    continue
                flows.append(
                    {
                        "record_id": f"sleeve-flow:{tag}:{pair_id}:{account}:{flow_class}",
                        "account_id": account,
                        "event_date": event_day,
                        "nav_recognition_date": recognition,
                        "flow_type": _SLEEVE_FLOW_TYPE,
                        "amount": {
                            "currency": "USD",
                            "status": "AVAILABLE",
                            "value": _format_decimal(signed),
                        },
                    }
                )
            if any(totals[key] != 0 or sizes[key] != 2 for key in totals):
                _fail()
            if role == "RESIDUAL":
                tolerance = _sleeve_money(
                    _sleeve_obj(payload.get("closure_policy")).get("tolerance_per_day")
                )
                for raw_day in _sleeve_seq(perimeter.get("days")):
                    row = _sleeve_obj(raw_day)
                    day = _date_value(row.get("day")).isoformat()
                    reconciliations.append(
                        {
                            "parent_account_id": parent,
                            "day": day,
                            "sleeves_nav": _format_decimal(
                                nav_totals.get(day, Decimal(0))
                            ),
                            "perimeter_nav": row.get("nav_end"),
                            "tolerance": _format_decimal(tolerance),
                        }
                    )
    return _SleeveView(
        accounts=tuple(sorted(accounts, key=lambda row: str(row["account_id"]))),
        assignments=tuple(
            sorted(
                assignments,
                key=lambda row: (str(row["account_id"]), str(row["effective_start"])),
            )
        ),
        residual=frozenset(residual),
        sleeve_accounts=frozenset(sleeve_accounts),
        perimeter_accounts=frozenset(perimeter_accounts),
        navs=tuple(sorted(navs, key=lambda row: str(row["record_id"]))),
        flows=tuple(sorted(flows, key=lambda row: str(row["record_id"]))),
        references=tuple(
            sorted(
                references,
                key=lambda row: (str(row["pair_id"]), str(row["account_id"])),
            )
        ),
        admissions=tuple(
            sorted(
                admissions,
                key=lambda row: (str(row["account_id"]), str(row["effective_start"])),
            )
        ),
        coverage=tuple(
            sorted(
                coverage,
                key=lambda row: (
                    str(_sleeve_obj(row["assignment"])["account_id"]),
                    str(_sleeve_obj(row["assignment"])["effective_start"]),
                ),
            )
        ),
        reconciliations=tuple(
            sorted(
                reconciliations,
                key=lambda row: (str(row["parent_account_id"]), str(row["day"])),
            )
        ),
    )


def _effective_perimeter_lists(
    selected: IndependentVerifierInputs,
    accounts: list[object],
    assignments: list[object],
) -> tuple[list[object], list[object]]:
    """Baseline accounts and assignments after the derived sleeves joined."""

    view = _independent_sleeve_view(selected)
    if view is None:
        return accounts, assignments
    effective: list[object] = []
    for raw in assignments:
        if not isinstance(raw, dict):
            _fail()
        row = dict(raw)
        if (
            row.get("account_id") in view.residual
            and row.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
        ):
            row["strategy_mapping_disposition"] = "HISTORICAL_POOL_ONLY"
            row["strategy_id"] = None
            if "mapping_status" in row:
                row["mapping_status"] = "UNMAPPED"
            if any(
                isinstance(other, dict)
                and other.get("account_id") == row["account_id"]
                and other.get("strategy_mapping_disposition") == "HISTORICAL_POOL_ONLY"
                and other.get("effective_end") == row.get("effective_start")
                for other in assignments
            ):
                row["effective_start"] = (
                    _date_value(row["effective_start"]) + timedelta(days=1)
                ).isoformat()
        effective.append(row)
    existing = {
        (
            row.get("account_id"),
            row.get("strategy_id"),
            row.get("effective_start"),
            row.get("effective_end"),
        )
        for row in effective
        if isinstance(row, dict)
    }
    for derived in view.assignments:
        key = (
            derived["account_id"],
            derived["strategy_id"],
            derived["effective_start"],
            derived["effective_end"],
        )
        if key in existing:
            _fail()
        effective.append(dict(derived))
    return [*accounts, *(dict(item) for item in view.accounts)], effective


def _ledger_with_derived(selected: IndependentVerifierInputs) -> dict[str, object]:
    """The provider ledger plus the derived sleeve facts, after the checks the
    derivation owes: sleeves reconcile to the parent and a parent-ledger
    reference is exactly one existing ledger flow."""

    ledger = _strict_object(selected.provider_ledger.contents)
    view = _independent_sleeve_view(selected)
    if view is None:
        return ledger
    navs = ledger.get("nav_observations")
    flows = ledger.get("flows")
    if not isinstance(navs, list) or not isinstance(flows, list):
        _fail()
    derived_accounts = view.sleeve_accounts | view.perimeter_accounts
    own_ids = {str(row["record_id"]) for row in (*view.navs, *view.flows)}
    for row in (*navs, *flows):
        if not isinstance(row, dict) or (
            row.get("account_id") in derived_accounts
            or str(row.get("record_id")) in own_ids
        ):
            _fail()
    nav_by_key: dict[tuple[object, object], object] = {}
    for row in navs:
        if isinstance(row, dict):
            nav_by_key[(row.get("account_id"), row.get("as_of_date"))] = row.get("nav")
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        for item in view.reconciliations:
            nav = nav_by_key.get((item["parent_account_id"], item["day"]))
            if (
                not isinstance(nav, dict)
                or nav.get("status") != "AVAILABLE"
                or abs(
                    _parse_retained_source_decimal(nav.get("value"))
                    - (
                        _parse_retained_source_decimal(item["sleeves_nav"])
                        + _parse_retained_source_decimal(item["perimeter_nav"])
                    )
                )
                > _parse_retained_source_decimal(item["tolerance"])
            ):
                _fail()
        for reference in view.references:
            wanted = _parse_retained_source_decimal(reference["amount"]) * (
                1 if reference["class"] == "INTERNAL_IN" else -1
            )
            found = [
                row
                for row in flows
                if isinstance(row, dict)
                and row.get("account_id") == reference["account_id"]
                and row.get("event_date") == reference["event_date"]
                and isinstance(row.get("amount"), dict)
                and row["amount"].get("status") == "AVAILABLE"
                and _parse_retained_source_decimal(row["amount"].get("value")) == wanted
            ]
            if len(found) != 1 or not str(found[0].get("flow_type")).startswith(
                "TRANSFER:INTERNAL:"
            ):
                _fail()
    merged = dict(ledger)
    merged["nav_observations"] = [*navs, *view.navs]
    merged["flows"] = [*flows, *view.flows]
    return merged


def _effective_admission_policy(
    selected: IndependentVerifierInputs, policy: dict[str, object]
) -> dict[str, object]:
    view = _independent_sleeve_view(selected)
    if view is None:
        return policy
    rows = policy.get("admissions")
    if not isinstance(rows, list):
        _fail()
    merged = dict(policy)
    merged["admissions"] = [
        *(
            row
            for row in rows
            if not (isinstance(row, dict) and row.get("account_id") in view.residual)
        ),
        *(dict(row) for row in view.admissions),
    ]
    return merged


def _sleeve_display(
    selected: IndependentVerifierInputs,
    display: Mapping[tuple[object, ...], dict[str, object]],
) -> dict[tuple[object, ...], dict[str, object]]:
    view = _independent_sleeve_view(selected)
    result = dict(display)
    if view is None:
        return result
    labels: dict[object, dict[str, object]] = {}
    for key, value in display.items():
        labels.setdefault(key[0], value)
    for item in view.coverage:
        epoch = _sleeve_obj(item["assignment"])
        parent = item["parent_account_id"]
        label = labels.get(parent)
        if label is None:
            _fail()
        suffix = str(epoch["account_id"]).removeprefix(f"{parent}-")
        result[tuple(epoch.values())] = {
            "account_label": f"{label['account_label']} / {suffix}",
            "last_four": label["last_four"],
        }
    return result


def _mixed_normalized_facts(
    selected: IndependentVerifierInputs,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    navs, _ = _normalize_provider_navs(
        selected, enforce_global_accounting_currency=False
    )
    ledger = _ledger_with_derived(selected)
    raw_flows = ledger.get("flows")
    raw_anchors = ledger.get("capital_anchors")
    if not isinstance(raw_flows, list) or not isinstance(raw_anchors, list):
        _fail()
    flows: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in raw_flows:
        if not isinstance(raw, dict) or not {
            "record_id",
            "account_id",
            "event_date",
            "flow_type",
            "amount",
        } <= set(raw):
            _fail()
        record_id = raw.get("record_id")
        amount = raw.get("amount")
        if (
            not isinstance(record_id, str)
            or not record_id
            or record_id in seen
            or not isinstance(amount, dict)
            or set(amount) != {"currency", "status", "value"}
            or not isinstance(amount.get("currency"), str)
            or re.fullmatch(r"[A-Z]{3,12}", amount["currency"]) is None
        ):
            _fail()
        seen.add(record_id)
        if amount.get("status") == "AVAILABLE":
            _parse_retained_source_decimal(amount.get("value"))
        elif amount.get("status") == "UNAVAILABLE":
            if amount.get("value") is not None:
                _fail()
        else:
            _fail()
        flow_type = raw.get("flow_type")
        if not isinstance(flow_type, str) or not flow_type:
            _fail()
        normalized_flow: dict[str, object] = {
            "record_id": record_id,
            "account_id": _internal_id(raw.get("account_id")),
            "event_date": _date_value(raw.get("event_date")).isoformat(),
            "nav_recognition_date": _date_value(
                raw.get("nav_recognition_date", raw.get("event_date"))
            ).isoformat(),
            "flow_type": flow_type,
            "amount": dict(amount),
        }
        normalized_flow.update(_retained_flow_scope_metadata(raw))
        provider_id = raw.get("provider_id")
        if provider_id is not None:
            if not isinstance(provider_id, str) or not provider_id.strip():
                _fail()
            normalized_flow["provider_id"] = provider_id.strip()
        generation_id = raw.get("generation_id")
        if generation_id is not None:
            if (
                not isinstance(generation_id, str)
                or _SHA256.fullmatch(generation_id) is None
            ):
                _fail()
            normalized_flow["generation_id"] = generation_id
        activity_id = raw.get("provider_activity_id_sha256")
        if activity_id is not None:
            if (
                not isinstance(activity_id, str)
                or _SHA256.fullmatch(activity_id) is None
                or "provider_id" not in normalized_flow
                or "generation_id" not in normalized_flow
            ):
                _fail()
            normalized_flow["provider_activity_id_sha256"] = activity_id
        flows.append(normalized_flow)
    anchors: list[dict[str, object]] = []
    anchor_ids: set[str] = set()
    common_anchor_fields = {
        "record_id",
        "account_id",
        "provider_id",
        "generation_id",
        "strategy_id",
        "mapping_status",
        "evidence_layer",
        "event_date",
        "nav_recognition_date",
        "capital_type",
        "amount",
        "source_artifact_id",
        "source_record_id",
        "mapping_basis",
    }
    for raw in raw_anchors:
        if not isinstance(raw, dict) or not common_anchor_fields <= set(raw):
            _fail()
        record_id = _fact_text(raw.get("record_id"))
        if record_id in anchor_ids:
            _fail()
        anchor_ids.add(record_id)
        capital_type = _fact_text(raw.get("capital_type"))
        if (
            capital_type
            not in {"OPENING_CAPITAL_ANCHOR", "STRATEGY_CAPITAL_ALLOCATION"}
            or raw.get("mapping_status") != "MAPPED"
            or raw.get("evidence_layer") != "ACCOUNT_LIVE"
        ):
            _fail()
        amount = raw.get("amount")
        if (
            not isinstance(amount, dict)
            or set(amount) != {"currency", "status", "value"}
            or type(amount.get("currency")) is not str
            or re.fullmatch(r"[A-Z]{3,12}", amount["currency"]) is None
            or amount.get("status") != "AVAILABLE"
            or _parse_retained_source_decimal(amount.get("value")) <= 0
        ):
            _fail()
        normalized = dict(raw)
        normalized.update(
            {
                "record_id": record_id,
                "account_id": _internal_id(raw.get("account_id")),
                "provider_id": _fact_text(raw.get("provider_id")),
                "generation_id": _fact_text(raw.get("generation_id")),
                "strategy_id": _fact_text(raw.get("strategy_id")),
                "event_date": _date_value(raw.get("event_date")).isoformat(),
                "nav_recognition_date": _date_value(
                    raw.get("nav_recognition_date")
                ).isoformat(),
                "capital_type": capital_type,
                "amount": dict(amount),
                "source_artifact_id": _fact_text(raw.get("source_artifact_id")),
                "source_record_id": _fact_text(raw.get("source_record_id")),
                "mapping_basis": _fact_text(raw.get("mapping_basis")),
            }
        )
        if _date_value(normalized["nav_recognition_date"]) < _date_value(
            normalized["event_date"]
        ):
            _fail()
        if capital_type == "OPENING_CAPITAL_ANCHOR":
            opening_nav = raw.get("opening_nav")
            if (
                not isinstance(opening_nav, dict)
                or set(opening_nav) != {"currency", "status", "value"}
                or _fact_currency(opening_nav.get("currency"))
                != opening_nav.get("currency")
                or opening_nav.get("status") != "AVAILABLE"
                or _parse_retained_source_decimal(opening_nav.get("value")) <= 0
            ):
                _fail()
            normalized["opening_nav"] = dict(opening_nav)
            accounting_currency = _fact_currency(opening_nav.get("currency"))
            source_currency = amount["currency"]
            event_fx = raw.get("event_time_fx")
            if source_currency == accounting_currency:
                if "event_time_fx" in raw:
                    _fail()
            else:
                expected_fx_fields = {
                    "base_currency",
                    "currency",
                    "date",
                    "field",
                    "generation_id",
                    "provider",
                    "rate",
                    "source_artifact_id",
                    "timing",
                }
                if (
                    accounting_currency != "USD"
                    or not isinstance(event_fx, dict)
                    or set(event_fx) != expected_fx_fields
                    or event_fx.get("base_currency") != accounting_currency
                    or event_fx.get("currency") != source_currency
                    or _date_value(event_fx.get("date"))
                    != _date_value(normalized["event_date"])
                    or event_fx.get("field") != "Close"
                    or event_fx.get("timing") != "UTC_DAILY_CLOSE"
                ):
                    _fail()
                fx_generation = event_fx.get("generation_id")
                if (
                    type(fx_generation) is not str
                    or _SHA256.fullmatch(fx_generation) is None
                ):
                    _fail()
                selected_fx = ledger.get("portfolio_fx_generation")
                if (
                    not isinstance(selected_fx, dict)
                    or set(selected_fx) != {"generation_id", "selection"}
                    or selected_fx.get("selection")
                    != "OWNER_SELECTED_YAHOO_DAILY_CLOSE_FOR_MATRIXPORT"
                ):
                    _fail()
                selected_fx_generation = selected_fx.get("generation_id")
                if (
                    type(selected_fx_generation) is not str
                    or _SHA256.fullmatch(selected_fx_generation) is None
                    or fx_generation != selected_fx_generation
                    or _fact_text(event_fx.get("provider")) != "yahoo"
                    or _fact_text(event_fx.get("source_artifact_id")) != "rates.csv"
                ):
                    _fail()
                rate_value = event_fx.get("rate")
                if type(rate_value) is not str:
                    _fail()
                try:
                    rate = Decimal(rate_value)
                except (InvalidOperation, ValueError):
                    _fail()
                if not rate.is_finite() or rate <= 0:
                    _fail()
            normalized["nav_source_artifact_id"] = _fact_text(
                raw.get("nav_source_artifact_id")
            )
        else:
            if "opening_nav" in raw:
                _fail()
            normalized["nav_source_artifact_id"] = _fact_text(
                raw.get("nav_source_artifact_id")
            )
        anchors.append(normalized)
    return navs, flows, anchors


def _mixed_cutoff_bounded_flows(
    selected: IndependentVerifierInputs,
    source_flows: list[dict[str, object]],
) -> list[dict[str, object]]:
    cutoff = _current_epoch_calculation_cutoff(selected)
    if cutoff is None:
        return source_flows
    return [
        flow
        for flow in source_flows
        if _date_value(flow["event_date"]) <= cutoff
        and _date_value(flow.get("nav_recognition_date", flow["event_date"])) <= cutoff
    ]


def _mixed_cutoff_bounded_capital_anchors(
    selected: IndependentVerifierInputs,
    source_anchors: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Project full source lineage onto the selected calculation cutoff."""

    cutoff = _current_epoch_calculation_cutoff(selected)
    if cutoff is None:
        return source_anchors
    return [
        anchor
        for anchor in source_anchors
        if _date_value(anchor["event_date"]) <= cutoff
        and (
            anchor.get("capital_type") == "STRATEGY_CAPITAL_ALLOCATION"
            or _date_value(anchor.get("nav_recognition_date", anchor["event_date"]))
            <= cutoff
        )
    ]


def _mixed_admitted_fact_projection(
    *,
    funded_assignments: list[Mapping[str, object]],
    routes: list[Mapping[str, object]],
    navs: list[dict[str, object]],
    flows: list[dict[str, object]],
    events: list[dict[str, object]],
    capital_anchors: list[dict[str, object]],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
]:
    """Independently select every funded root and exact internal support legs."""

    funded_keys = {
        tuple(_assignment_record(assignment).values())
        for assignment in funded_assignments
    }
    funded_routes = [
        route
        for route in routes
        if tuple(_assignment_record(route["selected_assignment"]).values())
        in funded_keys
    ]

    def in_interval(value: object, route: Mapping[str, object]) -> bool:
        fact_date = _date_value(value)
        receipt = route["receipt"]
        return (
            _date_value(receipt["requested_start"])
            <= fact_date
            <= _date_value(receipt["requested_end"])
        )

    def in_funded_epoch(fact: Mapping[str, object], field: str) -> bool:
        return any(
            fact.get("account_id") == route["selected_assignment"]["account_id"]
            and in_interval(fact.get(field), route)
            for route in funded_routes
        )

    excluded_account_only_source_ids = {
        str(flow["record_id"])
        for flow in flows
        if flow.get("provider_account_scope") == "ACCOUNT_ONLY"
        and flow.get("mapping_status") == "UNMAPPED"
        and "strategy_id" in flow
        and flow["strategy_id"] is None
    }
    root_events = [
        event
        for event in events
        if isinstance(event, dict)
        and in_funded_epoch(event, "nav_recognition_date")
        and str(event.get("source_record_id")) not in excluded_account_only_source_ids
    ]
    if any(event.get("classification_status") != "CLASSIFIED" for event in root_events):
        _fail()
    events_by_source_id = {
        str(event.get("source_record_id")): event
        for event in events
        if isinstance(event, dict)
    }
    supporting_ids: set[str] = set()
    for event in root_events:
        if event.get("disposition") == "INTERNAL":
            counterparty_id = event.get("counterparty_source_record_id")
            counterpart = events_by_source_id.get(str(counterparty_id))
            if (
                not isinstance(counterparty_id, str)
                or not isinstance(counterpart, dict)
                or counterpart.get("classification_status") != "CLASSIFIED"
                or counterpart.get("disposition") != "INTERNAL"
                or counterpart.get("counterparty_source_record_id")
                != event.get("source_record_id")
            ):
                _fail()
            supporting_ids.add(counterparty_id)
        elif event.get("disposition") != "EXTERNAL":
            _fail()
    projected_source_ids = {
        str(event["source_record_id"]) for event in root_events
    } | supporting_ids
    projected_events = [
        event
        for event in events
        if isinstance(event, dict)
        and str(event.get("source_record_id")) in projected_source_ids
    ]
    projected_flows = [
        flow for flow in flows if str(flow.get("record_id")) in projected_source_ids
    ]
    if (
        {str(event.get("source_record_id")) for event in projected_events}
        != projected_source_ids
        or {str(flow.get("record_id")) for flow in projected_flows}
        != projected_source_ids
        or any(
            event.get("classification_status") != "CLASSIFIED"
            for event in projected_events
        )
    ):
        _fail()
    # R1: each funded epoch opens at its account's last observation before the
    # funded start, so that one observation (of any status) is projected too.
    opening_record_ids: set[object] = set()
    for route in funded_routes:
        route_account = route["selected_assignment"]["account_id"]
        route_start = _date_value(route["selected_assignment"]["effective_start"])
        prior = [
            nav
            for nav in navs
            if nav.get("account_id") == route_account
            and _date_value(nav.get("as_of_date")) < route_start
        ]
        if prior:
            opening_record_ids.add(
                max(prior, key=lambda nav: _date_value(nav.get("as_of_date"))).get(
                    "record_id"
                )
            )
    projected_navs = [
        nav
        for nav in navs
        if in_funded_epoch(nav, "as_of_date")
        or nav.get("record_id") in opening_record_ids
    ]

    def anchor_membership_date(anchor: Mapping[str, object]) -> object:
        if anchor.get("capital_type") == "STRATEGY_CAPITAL_ALLOCATION":
            return anchor.get("event_date")
        return anchor.get("nav_recognition_date", anchor.get("event_date"))

    projected_anchors = [
        anchor
        for anchor in capital_anchors
        if any(
            anchor.get("account_id") == route["selected_assignment"]["account_id"]
            and anchor.get("strategy_id") == route["selected_assignment"]["strategy_id"]
            and in_interval(anchor_membership_date(anchor), route)
            for route in funded_routes
        )
    ]
    descriptor = {
        "selector": "ALL_FUNDED_EPOCHS_V1",
        "funded_epochs": [
            _assignment_record(route["selected_assignment"]) for route in funded_routes
        ],
        "supporting_internal_counterparty_source_record_ids": sorted(supporting_ids),
    }
    return (
        projected_navs,
        projected_flows,
        projected_events,
        projected_anchors,
        descriptor,
    )


def _mixed_projection_accounting_currency(
    navs: list[dict[str, object]],
    flows: list[dict[str, object]],
    capital_anchors: list[dict[str, object]],
) -> str:
    """Independently derive currency from exactly the projected calculation facts."""

    currencies: set[str] = set()
    for nav in navs:
        amount = nav.get("nav")
        if not isinstance(amount, dict):
            _fail()
        if amount.get("status") == "AVAILABLE":
            currency = amount.get("currency")
            if not isinstance(currency, str):
                _fail()
            currencies.add(currency)
    if len(currencies) != 1:
        _fail()
    accounting_currency = next(iter(currencies))
    for flow in flows:
        amount = flow.get("amount")
        if not isinstance(amount, dict):
            _fail()
        if (
            amount.get("status") == "AVAILABLE"
            and amount.get("currency") != accounting_currency
        ):
            _fail()
    for anchor in capital_anchors:
        money = (
            anchor.get("opening_nav")
            if anchor.get("capital_type") == "OPENING_CAPITAL_ANCHOR"
            else anchor.get("amount")
        )
        if (
            not isinstance(money, dict)
            or money.get("status") != "AVAILABLE"
            or money.get("currency") != accounting_currency
        ):
            _fail()
    return accounting_currency


def _verify_mixed_calculation_bridge(
    selected: IndependentVerifierInputs,
    normalized: Mapping[str, bytes],
    mapped: list[dict[str, object]],
    providers: Mapping[str, str],
    mapped_identity: Mapping[str, object],
    authority_state: Mapping[str, object],
    admission: Mapping[str, object],
) -> dict[str, object]:
    navs, source_flows, source_capital_anchors = _mixed_normalized_facts(selected)
    flows = _mixed_cutoff_bounded_flows(selected, source_flows)
    classified = _strict_object(normalized["authority/classified-flows.json"])
    events = classified.get("events")
    if (
        not isinstance(events, list)
        or classified.get("schema_version") != 5
        or classified.get("capital_anchors") != source_capital_anchors
    ):
        _fail()
    calculation_capital_anchors = _mixed_cutoff_bounded_capital_anchors(
        selected, source_capital_anchors
    )
    (
        projected_navs,
        projected_flows,
        projected_events,
        projected_anchors,
        projection,
    ) = _mixed_admitted_fact_projection(
        funded_assignments=admission["funded_assignments"],
        routes=_independent_calculation_routes(
            selected, mapped, authority_state["routes"]
        ),
        navs=navs,
        flows=flows,
        events=events,
        capital_anchors=calculation_capital_anchors,
    )
    currency = _mixed_projection_accounting_currency(
        projected_navs, projected_flows, projected_anchors
    )
    calculation_cutoff = _current_epoch_calculation_cutoff(selected)
    if calculation_cutoff is None:
        _fail()
    calculation_ledger = {
        "accounting_currency": currency,
        "calculation_cutoff_date": calculation_cutoff.isoformat(),
        "nav_observations": projected_navs,
        "flows": projected_flows,
        "capital_anchors": projected_anchors,
    }
    selected_ledger = _strict_object(selected.provider_ledger.contents)
    if "portfolio_fx_generation" in selected_ledger:
        calculation_ledger["portfolio_fx_generation"] = selected_ledger[
            "portfolio_fx_generation"
        ]
    ledger_bytes = canonical_json_bytes(calculation_ledger)
    ledger_artifact = _subject_artifact(
        ledger_bytes,
        "authority/calculation-ledger.json",
        "account-manifest-calculation-ledger",
        "1",
        selected.generation_as_of_date.isoformat(),
    )
    ledger_pair = {
        "generation_id": hashlib.sha256(ledger_bytes).hexdigest(),
        "artifact": ledger_artifact,
    }
    ledger_identity = {"schema_version": 1, **ledger_pair}
    policy = _strict_object(selected.flow_classification_policy.contents)
    policy_bytes = canonical_json_bytes(policy)
    calculation_events: list[dict[str, object]] = []
    for event in projected_events:
        if (
            not isinstance(event, dict)
            or event.get("classification_status") != "CLASSIFIED"
            or event.get("blocker_code") is not None
            or event.get("disposition") not in {"EXTERNAL", "INTERNAL"}
        ):
            _fail()
        amount = event.get("amount")
        if (
            not isinstance(amount, dict)
            or amount.get("status") != "AVAILABLE"
            or not isinstance(amount.get("value"), str)
        ):
            _fail()
        projected = {
            "event_id": event.get("event_id"),
            "source_record_id": event.get("source_record_id"),
            "account_id": event.get("account_id"),
            "event_date": event.get("event_date"),
            "nav_recognition_date": event.get("nav_recognition_date"),
            "amount": amount["value"],
            "disposition": event["disposition"],
        }
        if "counterparty_source_record_id" in event:
            projected["counterparty_source_record_id"] = event[
                "counterparty_source_record_id"
            ]
        calculation_events.append(projected)
    calculation_classified = {
        "schema_version": 4,
        "artifact_id": "authority/calculation-classified-flows.json",
        "as_of_date": selected.generation_as_of_date.isoformat(),
        "ledger_identity": ledger_pair,
        "classification_policy": {
            "policy_id": policy.get("policy_id"),
            "policy_version": policy.get("policy_version"),
            "policy_as_of_date": policy.get("as_of_date"),
            "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        },
        "events": calculation_events,
        "capital_anchors": projected_anchors,
    }
    calculation_classified_bytes = canonical_json_bytes(calculation_classified)
    calculation_classified_artifact = _subject_artifact(
        calculation_classified_bytes,
        "authority/calculation-classified-flows.json",
        "classified-flows",
        "4",
        selected.generation_as_of_date.isoformat(),
    )
    calculation_coverage = _mixed_coverage_payload(
        selected=selected,
        mapped=mapped,
        ledger_identity=ledger_pair,
        classified_artifact=calculation_classified_artifact,
        mapped_identity=mapped_identity,
        routes=authority_state["routes"],
    )

    tail_keys = _independent_tail_keys(selected)

    def in_interval(value: object, assignment: Mapping[str, object]) -> bool:
        fact_date = _date_value(value)
        start = _date_value(assignment["effective_start"])
        end_value = assignment["effective_end"]
        end = (
            None
            if end_value is None or _is_tail(assignment, tail_keys)
            else _date_value(end_value)
        )
        return start <= fact_date and (end is None or fact_date <= end)

    rows = [
        {
            "epoch": _assignment_record(assignment),
            "provider": providers[str(assignment["account_id"])],
            "nav_observations": [
                nav
                for nav in projected_navs
                if nav["account_id"] == assignment["account_id"]
                and in_interval(nav["as_of_date"], assignment)
            ],
            "classified_flows": [
                event
                for event in projected_events
                if isinstance(event, dict)
                and event.get("account_id") == assignment["account_id"]
                and in_interval(
                    event.get("nav_recognition_date", event.get("event_date")),
                    assignment,
                )
            ],
            "capital_anchors": [
                anchor
                for anchor in projected_anchors
                if anchor.get("account_id") == assignment["account_id"]
                and anchor.get("strategy_id") == assignment["strategy_id"]
                and in_interval(
                    anchor.get("nav_recognition_date", anchor.get("event_date")),
                    assignment,
                )
            ],
        }
        for assignment in mapped
    ]
    selected_identity = _selected_identity(selected)
    provider_identity = selected_identity["provider_ledger"]
    ledger_source = {
        "generation_id": provider_identity["generation_id"],
        "artifact": provider_identity["artifact"],
        "identity_artifact": provider_identity["generation_manifest"],
    }
    expected = {
        "schema_version": 4,
        "artifact_id": "authority/calculation-inputs.json",
        "as_of_date": selected.generation_as_of_date.isoformat(),
        "accounting_currency": currency,
        "source_ledger_identity": ledger_source,
        "source_classified_flows_artifact": authority_state["classified_artifact"],
        "mapped_assignments_identity": dict(mapped_identity),
        "projection": projection,
        "rows": rows,
        "calculation_ledger": calculation_ledger,
        "calculation_ledger_identity": ledger_identity,
        "calculation_classified_flows": calculation_classified,
        "calculation_flow_coverage": calculation_coverage,
        "derivation": "FUNDED_EPOCH_FACT_PROJECTION_V2",
    }
    if _strict_object(normalized["authority/calculation-inputs.json"]) != expected:
        _fail()
    return {
        "payload": expected,
        "ledger_source": ledger_source,
        "ledger": calculation_ledger,
        "events": calculation_events,
        "capital_anchors": projected_anchors,
        "currency": currency,
    }


def _selected_mixed_admission_bytes(
    selected: IndependentVerifierInputs,
    mapped: list[dict[str, object]],
) -> tuple[bytes, dict[str, object]]:
    """Independently project admission authority to the crosswalk subject set."""

    source = _effective_admission_policy(
        selected, _strict_object(_require_snapshot(selected.admission_policy).contents)
    )
    selected_keys = {
        tuple(_assignment_record(assignment).values()) for assignment in mapped
    }
    admissions = source.get("admissions")
    if not isinstance(admissions, list):
        _fail()
    policy = dict(source)
    policy["admissions"] = [
        dict(row)
        for row in admissions
        if isinstance(row, dict)
        and tuple(
            row.get(field)
            for field in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        )
        in selected_keys
    ]
    records = sorted(
        (_assignment_record(assignment) for assignment in mapped),
        key=lambda row: (
            str(row["account_id"]),
            str(row["strategy_id"]),
            str(row["effective_start"]),
            str(row["effective_end"] or "9999-12-31"),
        ),
    )
    policy["assignment_sha256"] = hashlib.sha256(
        json.dumps(
            records, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()
    return canonical_json_bytes(policy), policy


def _independent_membership_census(
    payload: Mapping[str, object],
) -> dict[tuple[str, str, str, str | None], list[str]]:
    """Index the governed current census without the producer's loader.

    The census names redacted sleeve aliases, not account ids, so an interval
    is identified by ``(provider, strategy, effective_start, effective_end)``
    and carries the multiset of its sleeves' membership statuses.  That is
    enough to bound how many account epochs of one interval can be funded
    (R2) without the private account crosswalk.
    """

    census = payload.get("current_census")
    sleeves = census.get("account_sleeves") if isinstance(census, Mapping) else None
    if not isinstance(sleeves, list):
        _fail()
    statuses: dict[tuple[str, str, str, str | None], list[str]] = {}
    for sleeve in sleeves:
        if not isinstance(sleeve, Mapping) or not isinstance(
            sleeve.get("assignments"), list
        ):
            _fail()
        provider = _fact_text(sleeve.get("provider_id")).upper()
        for assignment in sleeve["assignments"]:
            if not isinstance(assignment, Mapping):
                _fail()
            status = _fact_text(assignment.get("status"))
            if status not in _CENSUS_MEMBERSHIP_STATUSES:
                _fail()
            end = assignment.get("effective_end")
            interval = (
                provider,
                _fact_text(assignment.get("strategy_id")),
                _parse_canonical_date(assignment.get("effective_start")).isoformat(),
                None if end is None else _parse_canonical_date(end).isoformat(),
            )
            statuses.setdefault(interval, []).append(status)
    return statuses


def _verify_mixed_admission(
    selected: IndependentVerifierInputs,
    normalized: Mapping[str, bytes],
    mapped: list[dict[str, object]],
    mapped_identity: Mapping[str, object],
) -> dict[str, object]:
    expected_bytes, policy = _selected_mixed_admission_bytes(selected, mapped)
    if normalized["authority/mapped-epoch-admission.json"] != expected_bytes:
        _fail()
    if (
        set(policy)
        != {
            "schema_version",
            "policy_id",
            "policy_version",
            "artifact_id",
            "as_of_date",
            "assignment_sha256",
            "admissions",
        }
        or policy.get("schema_version") != 2
        or policy.get("artifact_id") != ("authority/mapped-epoch-admission.json")
        or policy.get("assignment_sha256") != mapped_identity.get("sha256")
    ):
        _fail()
    if (
        policy.get("policy_id") != "MAPPED_EPOCH_ADMISSION_V2"
        or policy.get("policy_version") != "2"
    ):
        _fail()
    admissions = policy.get("admissions")
    if not isinstance(admissions, list) or len(admissions) != len(mapped):
        _fail()
    mapped_by_key = {tuple(_assignment_record(row).values()): row for row in mapped}
    admission_by_key: dict[tuple[object, ...], dict[str, object]] = {}
    funded: list[dict[str, object]] = []
    navs, _flows, _anchors = _mixed_normalized_facts(selected)
    nav_by_id = {nav.get("record_id"): nav for nav in navs if isinstance(nav, dict)}
    for raw in admissions:
        if not isinstance(raw, dict):
            _fail()
        key = tuple(
            raw.get(name)
            for name in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        )
        if key not in mapped_by_key or key in admission_by_key:
            _fail()
        status = raw.get("status")
        continuity = raw.get("continuity_to_successor")
        if type(continuity) is not bool:
            _fail()
        if status == "FUNDED":
            funded_keys = {
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
                "status",
                "continuity_to_successor",
                "authority_kind",
                "funded_nav_record_ids",
            }
            if (
                not funded_keys <= set(raw) <= funded_keys | {"funded_end"}
                or raw.get("authority_kind") != "LEDGER_AVAILABLE_POSITIVE_NAV_V1"
            ):
                _fail()
            if "funded_end" in raw:
                # The last funded day of a liquidated and transferred account.
                funded_end_day = _parse_canonical_date(raw["funded_end"])
                assignment_start = _date_value(mapped_by_key[key]["effective_start"])
                assignment_stop = mapped_by_key[key]["effective_end"]
                if funded_end_day < assignment_start or (
                    assignment_stop is not None
                    and funded_end_day > _date_value(assignment_stop)
                ):
                    _fail()
            ids = raw.get("funded_nav_record_ids")
            if (
                not isinstance(ids, list)
                or not ids
                or any(type(record_id) is not str or not record_id for record_id in ids)
                or len(ids) != len(set(ids))
            ):
                _fail()
            assignment = mapped_by_key[key]
            start = _date_value(assignment["effective_start"])
            end = (
                _selected_date(selected.provider_ledger)
                if assignment["effective_end"] is None
                else _date_value(assignment["effective_end"])
            )
            for record_id in ids:
                nav = nav_by_id.get(record_id)
                if (
                    not isinstance(nav, dict)
                    or nav.get("account_id") != assignment["account_id"]
                ):
                    _fail()
                nav_date = _date_value(nav.get("as_of_date"))
                amount = nav.get("nav")
                if (
                    not isinstance(amount, dict)
                    or amount.get("status") != "AVAILABLE"
                    or _parse_retained_source_decimal(amount.get("value")) <= 0
                    or not start <= nav_date <= end
                ):
                    _fail()
            funded.append(raw)
        elif status == "BLOCKED":
            if (
                set(raw)
                != {
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                    "status",
                    "continuity_to_successor",
                    "reason",
                }
                or continuity
                or not isinstance(raw.get("reason"), str)
                or not raw["reason"]
            ):
                _fail()
        else:
            _fail()
        admission_by_key[key] = raw
    if set(admission_by_key) != set(mapped_by_key):
        _fail()
    baseline = _strict_object(selected.baseline.contents)
    full_assignments = baseline.get("assignments")
    if not isinstance(full_assignments, list):
        _fail()
    full_by_account: dict[str, list[dict[str, object]]] = {}
    for raw_assignment in full_assignments:
        if not isinstance(raw_assignment, dict):
            _fail()
        if raw_assignment.get("strategy_mapping_disposition") != "STRATEGY_MAPPED":
            continue
        account_id = raw_assignment.get("account_id")
        start = raw_assignment.get("effective_start")
        end = raw_assignment.get("effective_end")
        if (
            not isinstance(account_id, str)
            or not isinstance(raw_assignment.get("strategy_id"), str)
            or not isinstance(start, str)
            or (end is not None and not isinstance(end, str))
        ):
            _fail()
        start_date = _date_value(start)
        if end is not None and _date_value(end) < start_date:
            _fail()
        full_by_account.setdefault(account_id, []).append(raw_assignment)
    for chain in full_by_account.values():
        chain.sort(key=lambda row: str(row["effective_start"]))
    # R2: only census FUNDED_MEMBERSHIP intervals may be admitted as funded.
    # Each census interval bounds how many account epochs of the same
    # provider, strategy and interval may be funded; every other funded
    # admission fails closed.  A FUNDED-to-ASSIGNMENT_ONLY membership
    # transition inside one strategy is judged per epoch: an epoch inside
    # the census funded interval stays admissible and only a funded epoch
    # of the post-transition interval is rejected.
    census_payload = _strict_object(
        _require_snapshot(selected.membership_census).contents
    )
    census = _independent_membership_census(census_payload)
    census_providers = _baseline_account_providers(
        _effective_perimeter_lists(
            selected,
            _sleeve_seq(baseline.get("accounts")),
            _sleeve_seq(baseline.get("assignments")),
        )[0]
    )
    funded_counts: dict[tuple[str, str, str, str | None], int] = {}
    for key, policy_row in admission_by_key.items():
        if policy_row.get("status") != "FUNDED":
            continue
        provider = census_providers.get(str(key[0]))
        if not isinstance(provider, str) or not provider:
            _fail()
        interval = (
            provider.upper(),
            _fact_text(key[1]),
            _parse_canonical_date(key[2]).isoformat(),
            None if key[3] is None else _parse_canonical_date(key[3]).isoformat(),
        )
        funded_counts[interval] = funded_counts.get(interval, 0) + 1
    for interval, count in funded_counts.items():
        interval_statuses = census.get(interval)
        if interval_statuses is None:
            _fail()
        if count > sum(status == "FUNDED_MEMBERSHIP" for status in interval_statuses):
            _fail()
    predecessor_by_key: dict[tuple[object, ...], dict[str, object]] = {}
    for key, policy_row in admission_by_key.items():
        if policy_row["continuity_to_successor"] is not True:
            continue
        assignment = mapped_by_key[key]
        end_value = assignment["effective_end"]
        if policy_row["status"] != "FUNDED" or not isinstance(end_value, str):
            _fail()
        chain = full_by_account.get(str(assignment["account_id"]))
        if not isinstance(chain, list):
            _fail()
        full_index = next(
            (
                index
                for index, row in enumerate(chain)
                if tuple(
                    row.get(name)
                    for name in (
                        "account_id",
                        "strategy_id",
                        "effective_start",
                        "effective_end",
                    )
                )
                == key
            ),
            None,
        )
        if full_index is None or full_index + 1 >= len(chain):
            _fail()
        successor = chain[full_index + 1]
        successor_key = tuple(
            successor.get(name)
            for name in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        )
        successor_policy = admission_by_key.get(successor_key)
        if (
            successor.get("strategy_mapping_disposition") != "STRATEGY_MAPPED"
            or _date_value(successor.get("effective_start"))
            != _date_value(end_value) + timedelta(days=1)
            or not isinstance(successor_policy, dict)
            or successor_policy.get("status") != "FUNDED"
            or successor_key in predecessor_by_key
        ):
            _fail()
        cited_ids = policy_row.get("funded_nav_record_ids")
        if not isinstance(cited_ids, list) or not any(
            isinstance(nav_by_id.get(record_id), dict)
            and nav_by_id[record_id].get("as_of_date") == end_value
            for record_id in cited_ids
        ):
            _fail()
        predecessor_by_key[successor_key] = assignment
    funded_keys = {
        tuple(
            row[name]
            for name in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        )
        for row in funded
    }
    return {
        "policy": policy,
        "by_key": admission_by_key,
        "funded": funded,
        "predecessor_by_key": predecessor_by_key,
        "funded_assignments": [
            assignment
            for assignment in mapped
            if tuple(_assignment_record(assignment).values()) in funded_keys
        ],
    }


def _mixed_numeric_facts(
    selected: IndependentVerifierInputs,
    assignment: Mapping[str, object],
    bridge: Mapping[str, object],
    *,
    funded_end: date | None = None,
) -> _IndependentEpochFacts:
    ledger = bridge["ledger"]
    events = bridge["events"]
    capital_anchors = bridge["capital_anchors"]
    currency = bridge["currency"]
    if (
        not isinstance(assignment, Mapping)
        or not isinstance(ledger, Mapping)
        or not isinstance(events, list)
        or not isinstance(capital_anchors, list)
        or type(currency) is not str
    ):
        _fail()
    effective_start = _date_value(assignment["effective_start"])
    nav_facts: list[dict[str, object]] = []
    navs = ledger.get("nav_observations")
    if not isinstance(navs, list):
        _fail()
    pre_start_nav: tuple[date, Decimal | None] | None = None
    for nav in navs:
        if not isinstance(nav, dict):
            _fail()
        if nav.get("account_id") != assignment["account_id"]:
            continue
        amount = nav.get("nav")
        nav_date = _date_value(nav.get("as_of_date"))
        if nav_date < effective_start and (
            pre_start_nav is None or nav_date > pre_start_nav[0]
        ):
            # R1: the last observation before the funded start, whatever its
            # status, is the only candidate opening before the epoch.
            pre_start_nav = (
                nav_date,
                (
                    _parse_retained_source_decimal(amount.get("value"))
                    if isinstance(amount, dict) and amount.get("status") == "AVAILABLE"
                    else None
                ),
            )
        if not isinstance(amount, dict) or amount.get("status") != "AVAILABLE":
            continue
        nav_facts.append(
            {
                "account_id": nav.get("account_id"),
                "as_of_date": nav.get("as_of_date"),
                "currency": amount.get("currency"),
                "value": amount.get("value"),
            }
        )
    flow_facts: list[dict[str, object]] = []
    for event in events:
        if (
            not isinstance(event, dict)
            or event.get("account_id") != assignment["account_id"]
        ):
            continue
        amount = _parse_retained_source_decimal(event.get("amount"))
        disposition = event.get("disposition")
        if disposition not in {"EXTERNAL", "INTERNAL"} or amount == 0:
            _fail()
        flow_facts.append(
            {
                "account_id": event["account_id"],
                "event_date": event.get("event_date"),
                "nav_recognition_date": event.get("nav_recognition_date"),
                "classification": f"{disposition}_{'IN' if amount > 0 else 'OUT'}",
                "currency": currency,
                "amount": _format_decimal(abs(amount)),
                "source_record_id": event.get("source_record_id"),
            }
        )
    epoch_anchors = [
        anchor
        for anchor in capital_anchors
        if isinstance(anchor, dict)
        and anchor.get("account_id") == assignment["account_id"]
        and anchor.get("strategy_id") == assignment["strategy_id"]
    ]
    opening_rows = [
        anchor
        for anchor in epoch_anchors
        if anchor.get("capital_type") == "OPENING_CAPITAL_ANCHOR"
    ]
    if len(opening_rows) > 1:
        _fail()
    opening_anchor: _IndependentCapitalAnchorFact | None = None
    if opening_rows:
        raw_opening = opening_rows[0]
        opening_nav = raw_opening.get("opening_nav")
        if (
            not isinstance(opening_nav, dict)
            or opening_nav.get("status") != "AVAILABLE"
            or opening_nav.get("currency") != currency
        ):
            _fail()
        opening_event = _date_value(raw_opening.get("event_date"))
        opening_recognition = _date_value(raw_opening.get("nav_recognition_date"))
        # The opening anchor dates no MWR leg, but its own fill field is
        # validated like every other anchor's.
        _independent_anchor_fill_date(raw_opening, opening_event, opening_recognition)
        opening_amount = _parse_retained_source_decimal(
            raw_opening.get("amount", {}).get("value")
            if isinstance(raw_opening.get("amount"), dict)
            else None
        )
        if raw_opening["amount"]["currency"] != currency:
            event_fx = raw_opening.get("event_time_fx")
            if not isinstance(event_fx, dict):
                _fail()
            rate_value = event_fx.get("rate")
            if type(rate_value) is not str:
                _fail()
            try:
                rate = Decimal(rate_value)
            except (InvalidOperation, ValueError):
                _fail()
            if not rate.is_finite() or rate <= 0:
                _fail()
            opening_amount *= rate
        opening_anchor = _IndependentCapitalAnchorFact(
            opening_event,
            opening_recognition,
            currency,
            opening_amount,
            _parse_retained_source_decimal(opening_nav.get("value")),
        )
    allocation_facts: list[_IndependentFlowFact] = []
    for allocation in epoch_anchors:
        if allocation.get("capital_type") == "OPENING_CAPITAL_ANCHOR":
            continue
        if allocation.get("capital_type") != "STRATEGY_CAPITAL_ALLOCATION":
            _fail()
        amount = allocation.get("amount")
        if (
            not isinstance(amount, dict)
            or amount.get("status") != "AVAILABLE"
            or amount.get("currency") != currency
        ):
            _fail()
        allocation_event = _date_value(allocation.get("event_date"))
        allocation_recognition = _date_value(allocation.get("nav_recognition_date"))
        if allocation_recognition < allocation_event:
            _fail()
        allocation_economic = _independent_anchor_fill_date(
            allocation, allocation_event, allocation_recognition
        )
        # A provider fund allocation is an exact-timestamp fill.
        allocation_facts.append(
            _IndependentFlowFact(
                allocation_event,
                "INTERNAL_IN",
                currency,
                _parse_retained_source_decimal(amount.get("value")),
                allocation_recognition,
                _fact_text(allocation.get("source_record_id")),
                _EXACT_TIMESTAMP,
                allocation_economic,
            )
        )
    cutoff = _current_epoch_calculation_cutoff(selected)
    if cutoff is None:
        _fail()
    assignment_end = (
        cutoff
        if assignment["effective_end"] is None
        or _is_tail(assignment, _independent_tail_keys(selected))
        else min(cutoff, _date_value(assignment["effective_end"]))
    )
    if funded_end is not None:
        # The return closes at the policy's last funded day (#539).
        funded_end = min(cutoff, funded_end)
    facts = _parse_epoch_facts(
        nav_facts,
        flow_facts,
        account_id=str(assignment["account_id"]),
        start=effective_start,
        end=assignment_end,
        _retained_money=True,
        _dual_date_trace=True,
        opening_anchor=opening_anchor,
        pre_start_nav=pre_start_nav,
    )
    for allocation in allocation_facts:
        if not facts.start <= allocation.event_date <= facts.end:
            _fail()
    combined_flows = tuple(
        sorted((*facts.flows, *allocation_facts), key=_IndependentFlowFact.order_key)
    )
    sleeve_view = _independent_sleeve_view(selected)
    return _IndependentEpochFacts(
        facts.account_id,
        facts.start,
        facts.end,
        facts.navs,
        combined_flows,
        facts.opening_anchor,
        facts.dual_date_trace,
        facts.pre_start_nav,
        funded_end,
        sleeve_view is not None
        and str(assignment["account_id"]) in sleeve_view.sleeve_accounts,
    )


def _verify_mixed_output_and_state(
    selected: IndependentVerifierInputs,
    normalized: Mapping[str, bytes],
    mapped: list[dict[str, object]],
    providers: Mapping[str, str],
    mapped_identity: Mapping[str, object],
    authority_state: Mapping[str, object],
    bridge: Mapping[str, object],
    admission: Mapping[str, object],
) -> dict[str, object]:
    output = _strict_object(normalized["outputs/account-strategy-manifest.json"])
    calculation_cutoff = _current_epoch_calculation_cutoff(selected)
    if calculation_cutoff is None:
        _fail()
    candidate_rows = output.get("rows")
    if not isinstance(candidate_rows, list) or len(candidate_rows) != len(mapped):
        _fail()
    candidate_by_key: dict[tuple[object, ...], dict[str, object]] = {}
    for row in candidate_rows:
        if not isinstance(row, dict):
            _fail()
        key = tuple(
            row.get(name)
            for name in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        )
        if key in candidate_by_key:
            _fail()
        candidate_by_key[key] = row
    mapped_keys = [tuple(_assignment_record(row).values()) for row in mapped]
    if set(candidate_by_key) != set(mapped_keys):
        _fail()
    display_by_key = (
        {}
        if selected.current_epoch_crosswalk is None
        else _sleeve_display(
            selected,
            _independent_crosswalk_display_by_epoch(selected.current_epoch_crosswalk),
        )
    )
    disposition_by_key = (
        {}
        if selected.current_epoch_crosswalk is None
        else _independent_crosswalk_dispositions_by_epoch(
            selected.current_epoch_crosswalk
        )
    )
    sleeve_view = _independent_sleeve_view(selected)
    if sleeve_view is not None and selected.current_epoch_crosswalk is not None:
        # a derived sleeve epoch is a funded product epoch of its own
        for derived in sleeve_view.coverage:
            disposition_by_key[tuple(_sleeve_obj(derived["assignment"]).values())] = (
                "INCLUDED_PRODUCT_EPOCH"
            )
    admission_by_key = admission["by_key"]
    coverage_rows = authority_state["coverage"]["rows"]
    coverage_by_key = {tuple(row["assignment"].values()): row for row in coverage_rows}
    _, source_flows, source_capital_anchors = _mixed_normalized_facts(selected)
    cutoff_excluded_opening_anchor_keys = {
        tuple(_assignment_record(assignment).values())
        for assignment in mapped
        if any(
            anchor.get("capital_type") == "OPENING_CAPITAL_ANCHOR"
            and anchor.get("account_id") == assignment["account_id"]
            and anchor.get("strategy_id") == assignment["strategy_id"]
            and (
                _date_value(anchor["event_date"]) > calculation_cutoff
                or _date_value(anchor.get("nav_recognition_date", anchor["event_date"]))
                > calculation_cutoff
            )
            for anchor in source_capital_anchors
        )
    }
    mixed_tail_keys = _independent_tail_keys(selected)
    cutoff_excluded_allocation_keys = {
        tuple(_assignment_record(assignment).values())
        for assignment in mapped
        if any(
            anchor.get("capital_type") == "STRATEGY_CAPITAL_ALLOCATION"
            and anchor.get("account_id") == assignment["account_id"]
            and anchor.get("strategy_id") == assignment["strategy_id"]
            and _date_value(assignment["effective_start"])
            <= _date_value(anchor["event_date"])
            <= calculation_cutoff
            and (
                assignment["effective_end"] is None
                or _is_tail(assignment, mixed_tail_keys)
                or _date_value(anchor["event_date"])
                <= _date_value(assignment["effective_end"])
            )
            and _date_value(anchor.get("nav_recognition_date", anchor["event_date"]))
            > calculation_cutoff
            for anchor in source_capital_anchors
        )
    }
    cutoff_excluded_flow_keys = {
        tuple(_assignment_record(assignment).values())
        for assignment in mapped
        if any(
            flow.get("account_id") == assignment["account_id"]
            and _date_value(assignment["effective_start"])
            <= _date_value(flow["event_date"])
            <= calculation_cutoff
            and (
                assignment["effective_end"] is None
                or _is_tail(assignment, mixed_tail_keys)
                or _date_value(flow["event_date"])
                <= _date_value(assignment["effective_end"])
            )
            and _date_value(flow.get("nav_recognition_date", flow["event_date"]))
            > calculation_cutoff
            for flow in source_flows
        )
    }
    expected_rows: list[dict[str, object]] = []
    comparison = _NumericComparison(0, 0, 0)
    blocker_chains: list[dict[str, object]] = []
    for assignment in _sorted_mixed_output_assignments(mapped):
        key = tuple(_assignment_record(assignment).values())
        candidate = candidate_by_key[key]
        policy_row = admission_by_key[key]
        coverage_row = coverage_by_key[key]
        base = {
            **_assignment_record(assignment),
            "provider": providers[str(assignment["account_id"])],
            "continuity_to_successor": policy_row["continuity_to_successor"],
            **display_by_key.get(key, {}),
        }
        epoch_disposition = disposition_by_key.get(key)
        blocked_lifecycle = _independent_epoch_lifecycle(
            assignment["effective_end"], None, epoch_disposition
        )
        if selected.current_epoch_crosswalk is not None and set(base) != {
            "account_id",
            "strategy_id",
            "effective_start",
            "effective_end",
            "provider",
            "continuity_to_successor",
            "account_label",
            "last_four",
        }:
            _fail()
        coverage_complete = coverage_row["status"] == "COMPLETE"
        admission_funded = policy_row["status"] == "FUNDED"
        if coverage_complete and admission_funded:
            if key in (
                cutoff_excluded_opening_anchor_keys
                | cutoff_excluded_allocation_keys
                | cutoff_excluded_flow_keys
            ):
                blocking_reason = (
                    "OPENING_ANCHOR_RECOGNITION_AFTER_CUTOFF"
                    if key in cutoff_excluded_opening_anchor_keys
                    else "FLOW_RECOGNITION_NOT_LINKED_TO_OBSERVED_INTERVAL"
                )
                expected = {
                    **base,
                    **blocked_lifecycle,
                    "admission_status": "FUNDED",
                    "admission_policy_reference": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                    "admission_authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                    "funded_nav_record_ids": list(policy_row["funded_nav_record_ids"]),
                    "status": "BLOCKED_RETURN_LINKAGE",
                    "blocking_reason": blocking_reason,
                    **_mwr_blocked_projection(
                        "BLOCKED_ADMISSION", blocking_reason, coverage="BLOCKED"
                    ),
                }
                if candidate != expected or _OUTPUT_FORBIDDEN_NUMERIC_FIELDS & set(
                    candidate
                ):
                    _fail()
                expected_rows.append(expected)
                blocker_chains.append(
                    {
                        "assignment": _assignment_record(assignment),
                        "coverage": {"status": "COMPLETE", "blocker_code": None},
                        "admission": {"status": "FUNDED", "blocker_code": None},
                        "output": {
                            "status": "BLOCKED_RETURN_LINKAGE",
                            "blocking_reason": blocking_reason,
                        },
                        "numeric_candidate": False,
                    }
                )
                continue
            policy_funded_end = policy_row.get("funded_end")
            facts = _mixed_numeric_facts(
                selected,
                assignment,
                bridge,
                funded_end=(
                    None
                    if policy_funded_end is None
                    else _date_value(policy_funded_end)
                ),
            )
            predecessor_assignment = admission["predecessor_by_key"].get(key)
            predecessor_fact: _IndependentPredecessorFact | None = None
            fact_form: object = facts
            if predecessor_assignment is not None:
                if facts.opening_anchor is not None:
                    _fail()
                predecessor_facts = _mixed_numeric_facts(
                    selected, predecessor_assignment, bridge
                )
                if not predecessor_facts.navs:
                    _fail()
                closing = predecessor_facts.navs[-1]
                predecessor_end = predecessor_assignment["effective_end"]
                if not isinstance(predecessor_end, str):
                    _fail()
                predecessor_fact = _IndependentPredecessorFact(
                    str(predecessor_assignment["account_id"]),
                    _date_value(predecessor_end),
                    closing.as_of_date,
                    closing.currency,
                    closing.value,
                    True,
                )
                fact_form = (predecessor_fact, facts)
            if not _recognition_legs_attach_to_observed_intervals(
                facts, predecessor_fact
            ):
                expected = {
                    **base,
                    **blocked_lifecycle,
                    "admission_status": "FUNDED",
                    "admission_policy_reference": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                    "admission_authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                    "funded_nav_record_ids": list(policy_row["funded_nav_record_ids"]),
                    "status": "BLOCKED_RETURN_LINKAGE",
                    "blocking_reason": (
                        "FLOW_RECOGNITION_NOT_LINKED_TO_OBSERVED_INTERVAL"
                    ),
                    **_mwr_blocked_projection(
                        "BLOCKED_ADMISSION",
                        "FLOW_RECOGNITION_NOT_LINKED_TO_OBSERVED_INTERVAL",
                        coverage="BLOCKED",
                    ),
                }
                if candidate != expected or _OUTPUT_FORBIDDEN_NUMERIC_FIELDS & set(
                    candidate
                ):
                    _fail()
                expected_rows.append(expected)
                blocker_chains.append(
                    {
                        "assignment": _assignment_record(assignment),
                        "coverage": {"status": "COMPLETE", "blocker_code": None},
                        "admission": {"status": "FUNDED", "blocker_code": None},
                        "output": {
                            "status": "BLOCKED_RETURN_LINKAGE",
                            "blocking_reason": (
                                "FLOW_RECOGNITION_NOT_LINKED_TO_OBSERVED_INTERVAL"
                            ),
                        },
                        "numeric_candidate": False,
                    }
                )
                continue
            ok_lifecycle = _independent_epoch_lifecycle(
                assignment["effective_end"],
                candidate.get("performance_closed_on"),
                epoch_disposition,
            )
            expected_base = {
                **base,
                **ok_lifecycle,
                "admission_status": "FUNDED",
                "admission_policy_reference": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "admission_authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "funded_nav_record_ids": list(policy_row["funded_nav_record_ids"]),
                "status": "OK",
            }
            expected_mwr = _derive_independent_mwr_projection(
                assignment, fact_form, bridge
            )
            if set(candidate) - _POST_LIQUIDATION_FIELDS != (
                set(expected_base)
                | _NUMERIC_PROJECTION_FIELDS
                | _MWR_FIELDS
                | _PERIOD_FIELDS
            ):
                _fail()
            expected_period = _independent_period_fields(
                _independent_mixed_numeric_row(fact_form), expected_mwr
            )
            if {field: candidate[field] for field in _PERIOD_FIELDS} != expected_period:
                _fail()
            projection = {
                field: candidate[field]
                for field in (
                    _NUMERIC_PROJECTION_FIELDS
                    | (set(candidate) & _POST_LIQUIDATION_FIELDS)
                )
            }
            row_comparison = _compare_mixed_retained_numeric_epoch(
                fact_form, projection
            )
            if row_comparison != _NumericComparison(1, 1, 0):
                _fail()
            comparison = _NumericComparison(
                comparison.candidate_count + row_comparison.candidate_count,
                comparison.comparison_count + row_comparison.comparison_count,
                comparison.discrepancy_count + row_comparison.discrepancy_count,
            )
            expected_rows.append(
                {**expected_base, **projection, **expected_mwr, **expected_period}
            )
            blocker_chains.append(
                {
                    "assignment": _assignment_record(assignment),
                    "coverage": {"status": "COMPLETE", "blocker_code": None},
                    "admission": {"status": "FUNDED", "blocker_code": None},
                    "output": {"status": "OK", "blocking_reason": None},
                    "numeric_candidate": True,
                }
            )
        elif not admission_funded:
            expected = {
                **base,
                **blocked_lifecycle,
                "admission_status": "BLOCKED",
                "admission_policy_reference": "NONE",
                "admission_authority_kind": "NONE",
                "funded_nav_record_ids": [],
                "status": "BLOCKED_ADMISSION",
                "blocking_reason": policy_row["reason"],
                **_mwr_blocked_projection(
                    "BLOCKED_ADMISSION", policy_row["reason"], coverage="BLOCKED"
                ),
            }
            if candidate != expected or _OUTPUT_FORBIDDEN_NUMERIC_FIELDS & set(
                candidate
            ):
                _fail()
            expected_rows.append(expected)
            blocker_chains.append(
                {
                    "assignment": _assignment_record(assignment),
                    "coverage": {
                        "status": coverage_row["status"],
                        "blocker_code": coverage_row["blocker_code"],
                    },
                    "admission": {
                        "status": "BLOCKED",
                        "blocker_code": policy_row["reason"],
                    },
                    "output": {
                        "status": "BLOCKED_ADMISSION",
                        "blocking_reason": policy_row["reason"],
                    },
                    "numeric_candidate": False,
                }
            )
        else:
            expected = {
                **base,
                **blocked_lifecycle,
                "admission_status": "FUNDED",
                "admission_policy_reference": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "admission_authority_kind": "LEDGER_AVAILABLE_POSITIVE_NAV_V1",
                "funded_nav_record_ids": list(policy_row["funded_nav_record_ids"]),
                "status": "BLOCKED_UNPROVABLE_OPENING_FLOWS",
                "blocking_reason": "FLOW_COMPLETENESS_NOT_PROVEN",
                **_mwr_blocked_projection(
                    "INCOMPLETE_FLOW_COVERAGE",
                    "FLOW_COMPLETENESS_NOT_PROVEN",
                    coverage="BLOCKED",
                ),
            }
            if candidate != expected or _OUTPUT_FORBIDDEN_NUMERIC_FIELDS & set(
                candidate
            ):
                _fail()
            expected_rows.append(expected)
            blocker_chains.append(
                {
                    "assignment": _assignment_record(assignment),
                    "coverage": {
                        "status": "BLOCKED",
                        "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
                    },
                    "admission": {"status": "FUNDED", "blocker_code": None},
                    "output": {
                        "status": "BLOCKED_UNPROVABLE_OPENING_FLOWS",
                        "blocking_reason": "FLOW_COMPLETENESS_NOT_PROVEN",
                    },
                    "numeric_candidate": False,
                }
            )
    selected_identity = _selected_identity(selected)
    generated_date = selected.generation_as_of_date.isoformat()
    policy = _strict_object(selected.flow_classification_policy.contents)
    policy_bytes = canonical_json_bytes(policy)
    authority = authority_state["authority"]
    authority_artifact = _subject_artifact(
        normalized[_MIXED_AUTHORITY_PATH],
        _MIXED_AUTHORITY_PATH,
        "account-interval-retrieval-receipt-authority",
        "1",
        str(authority["as_of_date"]),
    )
    coverage_artifact = _subject_artifact(
        normalized["authority/account-flow-coverage.json"],
        "authority/account-flow-coverage.json",
        "account-flow-coverage",
        "2",
        _selected_date(selected.provider_ledger).isoformat(),
    )
    admission_policy = admission["policy"]
    expected_fixed = {
        "schema_version": 10,
        "lifecycle_schema_version": 1,
        "artifact": "account-strategy-manifest",
        "calculation_cutoff_date": calculation_cutoff.isoformat(),
        "accounting_currency": bridge["currency"],
        "authority_readiness": {
            "authority_status": "SOURCE_BOUND_CANDIDATE",
            "independent_authority_verification": "NOT_PERFORMED",
            "publication_readiness": "NOT_READY",
        },
        "source_identities": {
            "ledger": bridge["ledger_source"],
            "baseline": {
                "generation_id": selected_identity["baseline"]["generation_id"],
                "artifact": selected_identity["baseline"]["artifact"],
            },
            "classified_flows": {
                "artifact": authority_state["classified_artifact"],
                "schema_version": 5,
                "ledger_identity": bridge["ledger_source"],
                "classification_policy": {
                    "policy_id": policy.get("policy_id"),
                    "policy_version": policy.get("policy_version"),
                    "policy_as_of_date": policy.get("as_of_date"),
                    "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
                },
            },
            "flow_classification_policy": {
                "policy_id": policy.get("policy_id"),
                "policy_version": policy.get("policy_version"),
                "event_dispositions": policy.get("event_dispositions"),
                "artifact": _subject_artifact(
                    policy_bytes,
                    "authority/flow-classification-policy.json",
                    "flow-classification-policy",
                    "1",
                    str(policy.get("as_of_date")),
                ),
            },
            "flow_coverage": {
                "artifact": coverage_artifact,
                "ledger_identity": bridge["ledger_source"],
                "classified_flows_artifact": authority_state["classified_artifact"],
                "receipt_authority": authority_artifact,
            },
            "admission_policy": {
                "policy_id": admission_policy["policy_id"],
                "policy_version": admission_policy["policy_version"],
                "artifact": _subject_artifact(
                    normalized["authority/mapped-epoch-admission.json"],
                    "authority/mapped-epoch-admission.json",
                    "account-strategy-manifest-admission-policy",
                    "2",
                    str(admission_policy["as_of_date"]),
                ),
                "assignment": dict(mapped_identity),
            },
            "assignment": dict(mapped_identity),
            "calculation_code": selected_identity["code_sources"]["epoch_producer"],
            "selected_inputs": _subject_artifact(
                normalized["authority/selected-input-identities.json"],
                "authority/selected-input-identities.json",
                "selected-input-identities",
                "1",
                generated_date,
            ),
            "account_assignment_perimeter": _subject_artifact(
                normalized["authority/account-assignment-perimeter.json"],
                "authority/account-assignment-perimeter.json",
                "account-assignment-perimeter",
                "1",
                generated_date,
            ),
            "calculation_inputs": _subject_artifact(
                normalized["authority/calculation-inputs.json"],
                "authority/calculation-inputs.json",
                "account-manifest-calculation-inputs",
                "4",
                generated_date,
            ),
            "code_identities": _subject_artifact(
                normalized["authority/code-identities.json"],
                "authority/code-identities.json",
                "account-manifest-code-identities",
                "1",
                generated_date,
            ),
        },
        "epoch_continuity": {
            "rule_id": "EXPLICIT_FUNDED_EPOCH_CONTINUITY_V1",
            "status": "POLICY_BOUND_ONLY",
            "rule": (
                "An epoch closing NAV opens only its immediately continuous, "
                "explicitly funded successor. A missing/blocked predecessor closing NAV "
                "blocks that successor; no capital is inferred."
            ),
        },
        "flow_reconciliation": {
            "status": "ONE_TO_ONE_VERIFIED",
            "method": "EXACT_RAW_RECORD_TO_CLASSIFIED_EVENT_BINDING",
        },
        "rows": expected_rows,
    }
    if output != expected_fixed:
        _fail()
    baseline = _strict_object(selected.baseline.contents)
    accounts = baseline.get("accounts")
    assignments = baseline.get("assignments")
    if not isinstance(accounts, list) or not isinstance(assignments, list):
        _fail()
    accounts, assignments = _effective_perimeter_lists(selected, accounts, assignments)
    disposition_counts = {
        disposition: sum(
            isinstance(row, dict)
            and row.get("strategy_mapping_disposition") == disposition
            for row in assignments
        )
        for disposition in (
            "STRATEGY_MAPPED",
            "HISTORICAL_POOL_ONLY",
            "RESERVE_NOT_APPLICABLE",
        )
    }
    admissions = admission_policy["admissions"]
    counts = {
        "selected_baseline_account_count": len(accounts),
        "account_count": len({row["account_id"] for row in assignments}),
        "assignment_count": len(assignments),
        "mapped_assignment_count": disposition_counts["STRATEGY_MAPPED"],
        "historical_assignment_count": disposition_counts["HISTORICAL_POOL_ONLY"],
        "reserve_assignment_count": disposition_counts["RESERVE_NOT_APPLICABLE"],
        "current_product_epoch_count": len(mapped),
        **_derive_independent_population_counts(selected, normalized),
        "calculation_input_row_count": len(bridge["payload"]["rows"]),
        "coverage_row_count": len(coverage_rows),
        "admission_row_count": len(admissions),
        "output_row_count": len(expected_rows),
        "complete_coverage_row_count": sum(
            row["status"] == "COMPLETE" for row in coverage_rows
        ),
        "funded_admission_row_count": sum(
            row["status"] == "FUNDED" for row in admissions
        ),
        "ok_output_row_count": sum(row["status"] == "OK" for row in expected_rows),
        "blocked_coverage_row_count": sum(
            row["status"] == "BLOCKED" for row in coverage_rows
        ),
        "blocked_admission_row_count": sum(
            row["status"] == "BLOCKED" for row in admissions
        ),
        "blocked_output_row_count": sum(row["status"] != "OK" for row in expected_rows),
    }
    # Report state has its own canonical chain order.  Coverage, calculation,
    # and admission remain in validated crosswalk order above; only the
    # state/report projection is normalized here for its strict key invariant.
    blocker_chains.sort(
        key=lambda chain: (
            str(chain["assignment"]["account_id"]),
            str(chain["assignment"]["strategy_id"]),
            str(chain["assignment"]["effective_start"]),
            str(chain["assignment"]["effective_end"] or ""),
        )
    )
    return {
        "counts": counts,
        "blocker_chains": blocker_chains,
        "numeric_verification": {
            "candidate_count": comparison.candidate_count,
            "comparison_count": comparison.comparison_count,
            "discrepancy_count": comparison.discrepancy_count,
            "disposition": (
                "INDEPENDENT_RECALCULATION_NO_DISCREPANCIES"
                if comparison.candidate_count
                else "NOT_APPLICABLE_NO_CANDIDATES"
            ),
        },
        "invariants": dict(_RETAINED_D2A_INVARIANTS),
    }


def _verify_mixed_semantics_normalized(
    selected: IndependentVerifierInputs,
    normalized: Mapping[str, bytes],
) -> dict[str, object]:
    b1_state = _verify_c4b1_contract_normalized(selected, normalized)
    _verify_c4b2_policy_and_flows(
        selected, normalized, enforce_global_accounting_currency=False
    )
    _verify_c4b3_code_identities(selected, normalized)
    perimeter = _strict_object(
        normalized["authority/account-assignment-perimeter.json"]
    )
    mapped = _selected_current_epoch_rows(selected, perimeter.get("rows"))
    baseline = _strict_object(selected.baseline.contents)
    effective_accounts, _ = _effective_perimeter_lists(
        selected,
        _sleeve_seq(baseline.get("accounts")),
        _sleeve_seq(baseline.get("assignments")),
    )
    providers = _baseline_account_providers(effective_accounts)
    mapped_identity = b1_state["mapped_assignments_identity"]
    authority_state = _verify_mixed_authority_and_coverage_many(
        selected, normalized, mapped, providers, mapped_identity
    )
    admission = _verify_mixed_admission(selected, normalized, mapped, mapped_identity)
    bridge = _verify_mixed_calculation_bridge(
        selected,
        normalized,
        mapped,
        providers,
        mapped_identity,
        authority_state,
        admission,
    )
    return _verify_mixed_output_and_state(
        selected,
        normalized,
        mapped,
        providers,
        mapped_identity,
        authority_state,
        bridge,
        admission,
    )


def _verify_retained_semantics(
    selected_snapshots: IndependentVerifierInputs,
    subject_payloads: Mapping[object, object],
) -> dict[str, object]:
    normalized = _normalize_subject_payloads(subject_payloads)
    return _verify_retained_semantics_normalized(selected_snapshots, normalized)


def _verify_retained_semantics_normalized(
    selected_snapshots: IndependentVerifierInputs,
    normalized: Mapping[str, bytes],
) -> dict[str, object]:
    b1_state = _verify_c4b1_contract_normalized(selected_snapshots, normalized)
    _verify_c4b2_policy_and_flows(selected_snapshots, normalized)
    _verify_c4b3_calculation_inputs(selected_snapshots, normalized, b1_state)
    _verify_c4b3_code_identities(selected_snapshots, normalized)
    _verify_c4b3_2a_coverage_admission(selected_snapshots, normalized, b1_state)
    _verify_c4b3_2b1_output(selected_snapshots, normalized, b1_state)
    return _derive_c4b3_2b2_fragments(selected_snapshots, normalized, b1_state)


_RETAINED_D2A_COUNT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "selected_baseline_account_count",
        "account_count",
        "assignment_count",
        "mapped_assignment_count",
        "historical_assignment_count",
        "reserve_assignment_count",
        "observed_nav_fact_count",
        "observed_raw_flow_count",
        "classified_event_count",
        "classified_event_classified_count",
        "classified_event_blocked_count",
        "unmatched_nav_fact_count",
        "unmatched_raw_flow_fact_count",
        "unmatched_fact_count",
        "unmatched_projection_count",
        "calculation_input_row_count",
        "coverage_row_count",
        "admission_row_count",
        "output_row_count",
        "blocked_coverage_row_count",
        "blocked_admission_row_count",
        "blocked_output_row_count",
    }
)
_RETAINED_D2A_INVARIANTS: Final[dict[str, bool]] = {
    "exact_subject_set": True,
    "canonical_subject_bytes": True,
    "selected_source_binding": True,
    "fact_disposition_complete": True,
    "unassigned_numeric_exclusion": True,
    "classified_flow_reconciliation": True,
    "mapped_row_bijection": True,
    "blocker_propagation": True,
    "blocked_numeric_exclusion": True,
    "privacy_safe_projection": True,
}


def _validate_retained_candidate_state(
    comparison: _NumericComparison,
    disposition: object,
    state: Mapping[str, object],
) -> None:
    """Validate the exact retained, genuinely non-numeric D2a state."""

    if type(comparison) is not _NumericComparison or type(state) is not dict:
        _fail()
    if set(state) != {"counts", "blocker_chains", "numeric_verification", "invariants"}:
        _fail()
    counts_object = state.get("counts")
    chains = state.get("blocker_chains")
    numeric = state.get("numeric_verification")
    invariants = state.get("invariants")
    if (
        type(counts_object) is not dict
        or set(counts_object) != _RETAINED_D2A_COUNT_KEYS
        or any(type(value) is not int or value < 0 for value in counts_object.values())
        or type(invariants) is not dict
        or invariants != _RETAINED_D2A_INVARIANTS
        or any(type(value) is not bool for value in invariants.values())
    ):
        _fail()
    if (
        counts_object["selected_baseline_account_count"]
        < counts_object["account_count"]
        or counts_object["assignment_count"]
        != counts_object["mapped_assignment_count"]
        + counts_object["historical_assignment_count"]
        + counts_object["reserve_assignment_count"]
        or counts_object["classified_event_count"]
        != counts_object["classified_event_classified_count"]
        + counts_object["classified_event_blocked_count"]
        or counts_object["classified_event_count"]
        != counts_object["observed_raw_flow_count"]
        or counts_object["unmatched_fact_count"]
        != counts_object["unmatched_nav_fact_count"]
        + counts_object["unmatched_raw_flow_fact_count"]
        or counts_object["unmatched_projection_count"]
        != counts_object["unmatched_nav_fact_count"]
        + 2 * counts_object["unmatched_raw_flow_fact_count"]
        or counts_object["unmatched_fact_count"]
        > counts_object["observed_nav_fact_count"]
        + counts_object["observed_raw_flow_count"]
        or any(
            counts_object[key] != counts_object["mapped_assignment_count"]
            for key in (
                "calculation_input_row_count",
                "coverage_row_count",
                "admission_row_count",
                "output_row_count",
                "blocked_coverage_row_count",
                "blocked_admission_row_count",
                "blocked_output_row_count",
            )
        )
    ):
        _fail()
    if (
        type(chains) is not list
        or len(chains) != counts_object["mapped_assignment_count"]
    ):
        _fail()
    prior_key: tuple[str, str, str, str] | None = None
    for chain in chains:
        if (
            type(chain) is not dict
            or set(chain)
            != {"assignment", "coverage", "admission", "output", "numeric_candidate"}
            or chain.get("numeric_candidate") is not False
        ):
            _fail()
        assignment = chain.get("assignment")
        coverage = chain.get("coverage")
        admission = chain.get("admission")
        output = chain.get("output")
        if (
            type(assignment) is not dict
            or set(assignment)
            != {"account_id", "strategy_id", "effective_start", "effective_end"}
            or type(assignment.get("account_id")) is not str
            or type(assignment.get("strategy_id")) is not str
            or type(assignment.get("effective_start")) is not str
            or (
                assignment.get("effective_end") is not None
                and type(assignment.get("effective_end")) is not str
            )
            or type(coverage) is not dict
            or coverage
            != {
                "status": "BLOCKED",
                "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
            }
            or type(admission) is not dict
            or admission
            != {
                "status": "BLOCKED",
                "blocker_code": "FLOW_COMPLETENESS_NOT_PROVEN",
            }
            or type(output) is not dict
            or output
            != {
                "status": "BLOCKED_ADMISSION",
                "blocking_reason": "FLOW_COMPLETENESS_NOT_PROVEN",
            }
        ):
            _fail()
        key = (
            assignment["account_id"],
            assignment["strategy_id"],
            assignment["effective_start"],
            assignment["effective_end"] or "",
        )
        if prior_key is not None and key <= prior_key:
            _fail()
        prior_key = key
    if type(numeric) is not dict or set(numeric) != {
        "candidate_count",
        "comparison_count",
        "discrepancy_count",
        "disposition",
    }:
        _fail()
    counts = (
        comparison.candidate_count,
        comparison.comparison_count,
        comparison.discrepancy_count,
    )
    if any(type(value) is not int or value < 0 for value in counts):
        _fail()
    candidate_count, comparison_count, discrepancy_count = counts
    if comparison_count > candidate_count or discrepancy_count > comparison_count:
        _fail()
    if discrepancy_count > 0:
        _fail()
    if candidate_count != 0 or comparison_count != 0:
        _fail()
    if (
        type(disposition) is not str
        or disposition != "NOT_APPLICABLE_NO_CANDIDATES"
        or tuple(
            numeric[key]
            for key in ("candidate_count", "comparison_count", "discrepancy_count")
        )
        != counts
        or numeric["disposition"] != disposition
    ):
        _fail()


def _validate_numeric_decision(
    comparison: _NumericComparison,
    disposition: object,
    state: Mapping[str, object],
) -> None:
    """Admit only the retained, genuinely non-numeric D2a disposition."""

    _validate_retained_candidate_state(comparison, disposition, state)


def _freeze_report_candidate(value: object) -> object:
    """Copy the internal candidate into recursively immutable containers."""

    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_report_candidate(item) for key, item in value.items()}
        )
    if type(value) is list or type(value) is tuple:
        return tuple(_freeze_report_candidate(item) for item in value)
    if type(value) is set or type(value) is frozenset:
        return frozenset(_freeze_report_candidate(item) for item in value)
    if type(value) in {str, bytes, int, bool, type(None)}:
        return value
    _fail()


def _build_retained_report_candidate(
    state: Mapping[str, object],
) -> Mapping[str, object]:
    """Return the pre-D3 candidate from derived retained semantics only."""

    if type(state) is not dict or set(state) != {
        "counts",
        "blocker_chains",
        "numeric_verification",
        "invariants",
    }:
        _fail()
    frozen = _freeze_report_candidate(
        {
            "counts": state["counts"],
            "blocker_chains": state["blocker_chains"],
            "numeric_verification": state["numeric_verification"],
            "invariants": state["invariants"],
        }
    )
    if not isinstance(frozen, Mapping):
        _fail()
    return frozen


def _thaw_retained_report_candidate(retained_candidate: object) -> dict[str, object]:
    """Copy only the exact frozen D2 candidate representation into JSON values."""

    containers: set[int] = set()

    def thaw(value: object) -> object:
        if type(value) is MappingProxyType:
            identity = id(value)
            if identity in containers:
                _fail()
            containers.add(identity)
            result: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    _fail()
                result[key] = thaw(item)
            return result
        if type(value) is tuple:
            identity = id(value)
            if identity in containers:
                _fail()
            containers.add(identity)
            return [thaw(item) for item in value]
        if type(value) in {str, int, bool, type(None)}:
            return value
        _fail()

    result = thaw(retained_candidate)
    if type(result) is not dict:
        _fail()
    return result


def _assemble_independent_report(
    selected: IndependentVerifierInputs,
    normalized_subjects: Mapping[str, bytes],
    retained_candidate: object,
) -> dict[str, object]:
    """Assemble the closed retained report from only derived D2 state."""

    if type(selected) is not IndependentVerifierInputs:
        _fail()
    thawed = _thaw_retained_report_candidate(retained_candidate)
    numeric = thawed.get("numeric_verification")
    if type(numeric) is not dict:
        _fail()
    try:
        comparison = _NumericComparison(
            numeric["candidate_count"],
            numeric["comparison_count"],
            numeric["discrepancy_count"],
        )
        disposition = numeric["disposition"]
    except KeyError:
        _fail()
    _validate_retained_candidate_state(comparison, disposition, thawed)
    verifier_artifact, selected_source_identities = _report_source_metadata(selected)
    subjects = _report_subject_entries(normalized_subjects)
    return {
        "schema_version": 2,
        "artifact_id": _REPORT_PATH,
        "as_of_date": selected.generation_as_of_date.isoformat(),
        "status": "VERIFIED",
        "verifier": verifier_artifact,
        "selected_source_identities": selected_source_identities,
        "subjects": subjects,
        "subjects_semantic_sha256": _report_subjects_semantic_sha256(subjects),
        "counts": thawed["counts"],
        "blocker_chains": thawed["blocker_chains"],
        "numeric_verification": numeric,
        "invariants": thawed["invariants"],
    }


_MIXED_COUNT_KEYS: Final[frozenset[str]] = _RETAINED_D2A_COUNT_KEYS | {
    "current_product_epoch_count",
    "complete_coverage_row_count",
    "funded_admission_row_count",
    "ok_output_row_count",
}


def _validate_mixed_candidate_state(state: Mapping[str, object]) -> None:
    if type(state) is not dict or set(state) != {
        "counts",
        "blocker_chains",
        "numeric_verification",
        "invariants",
    }:
        _fail()
    counts = state.get("counts")
    chains = state.get("blocker_chains")
    numeric = state.get("numeric_verification")
    if (
        type(counts) is not dict
        or set(counts) != _MIXED_COUNT_KEYS
        or any(type(value) is not int or value < 0 for value in counts.values())
        or state.get("invariants") != _RETAINED_D2A_INVARIANTS
        or type(chains) is not list
        or len(chains) != counts["current_product_epoch_count"]
        or type(numeric) is not dict
        or set(numeric)
        != {
            "candidate_count",
            "comparison_count",
            "discrepancy_count",
            "disposition",
        }
    ):
        _fail()
    candidate_count = numeric["candidate_count"]
    if (
        type(candidate_count) is not int
        or candidate_count < 0
        or numeric["comparison_count"] != candidate_count
        or numeric["discrepancy_count"] != 0
        or numeric["disposition"]
        != (
            "INDEPENDENT_RECALCULATION_NO_DISCREPANCIES"
            if candidate_count
            else "NOT_APPLICABLE_NO_CANDIDATES"
        )
    ):
        _fail()
    if (
        counts["assignment_count"]
        != counts["mapped_assignment_count"]
        + counts["historical_assignment_count"]
        + counts["reserve_assignment_count"]
        or counts["classified_event_count"]
        != counts["classified_event_classified_count"]
        + counts["classified_event_blocked_count"]
        or counts["classified_event_count"] != counts["observed_raw_flow_count"]
        or counts["unmatched_fact_count"]
        != counts["unmatched_nav_fact_count"] + counts["unmatched_raw_flow_fact_count"]
        or counts["unmatched_projection_count"]
        != counts["unmatched_nav_fact_count"]
        + 2 * counts["unmatched_raw_flow_fact_count"]
        or any(
            counts[key] != counts["current_product_epoch_count"]
            for key in (
                "calculation_input_row_count",
                "coverage_row_count",
                "admission_row_count",
                "output_row_count",
            )
        )
        or counts["coverage_row_count"]
        != counts["complete_coverage_row_count"] + counts["blocked_coverage_row_count"]
        or counts["admission_row_count"]
        != counts["funded_admission_row_count"] + counts["blocked_admission_row_count"]
        or counts["output_row_count"]
        != counts["ok_output_row_count"] + counts["blocked_output_row_count"]
        or counts["ok_output_row_count"] != candidate_count
        or counts["ok_output_row_count"]
        > min(
            counts["complete_coverage_row_count"],
            counts["funded_admission_row_count"],
        )
    ):
        _fail()
    numeric_chains = 0
    prior: tuple[str, str, str, str] | None = None
    for chain in chains:
        if type(chain) is not dict or set(chain) != {
            "assignment",
            "coverage",
            "admission",
            "output",
            "numeric_candidate",
        }:
            _fail()
        assignment = chain.get("assignment")
        if not isinstance(assignment, dict) or set(assignment) != {
            "account_id",
            "strategy_id",
            "effective_start",
            "effective_end",
        }:
            _fail()
        key = (
            str(assignment["account_id"]),
            str(assignment["strategy_id"]),
            str(assignment["effective_start"]),
            str(assignment["effective_end"] or ""),
        )
        if prior is not None and key <= prior:
            _fail()
        prior = key
        if chain["numeric_candidate"] is True:
            numeric_chains += 1
            if (
                chain["coverage"] != {"status": "COMPLETE", "blocker_code": None}
                or chain["admission"] != {"status": "FUNDED", "blocker_code": None}
                or chain["output"] != {"status": "OK", "blocking_reason": None}
            ):
                _fail()
        elif chain["numeric_candidate"] is False:
            coverage = chain["coverage"]
            admission = chain["admission"]
            output = chain["output"]
            if not isinstance(coverage, dict) or not isinstance(admission, dict):
                _fail()
            if coverage not in (
                {"status": "COMPLETE", "blocker_code": None},
                {
                    "status": "BLOCKED",
                    "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
                },
            ):
                _fail()
            if admission.get("status") == "BLOCKED":
                if (
                    not isinstance(admission.get("blocker_code"), str)
                    or not admission["blocker_code"]
                    or output
                    != {
                        "status": "BLOCKED_ADMISSION",
                        "blocking_reason": admission["blocker_code"],
                    }
                ):
                    _fail()
            elif admission == {"status": "FUNDED", "blocker_code": None}:
                permitted_output = (
                    {
                        "status": "BLOCKED_RETURN_LINKAGE",
                        "blocking_reason": (
                            "FLOW_RECOGNITION_NOT_LINKED_TO_OBSERVED_INTERVAL"
                        ),
                    }
                    if coverage["status"] == "COMPLETE"
                    else {
                        "status": "BLOCKED_UNPROVABLE_OPENING_FLOWS",
                        "blocking_reason": "FLOW_COMPLETENESS_NOT_PROVEN",
                    }
                )
                if output != permitted_output:
                    _fail()
            else:
                _fail()
        else:
            _fail()
    if numeric_chains != candidate_count:
        _fail()


def _assemble_mixed_report(
    selected: IndependentVerifierInputs,
    normalized_subjects: Mapping[str, bytes],
    retained_candidate: object,
) -> dict[str, object]:
    thawed = _thaw_retained_report_candidate(retained_candidate)
    _validate_mixed_candidate_state(thawed)
    verifier_artifact, selected_source_identities = _report_source_metadata(selected)
    subjects = _selected_report_subject_entries(selected, normalized_subjects)
    return {
        "schema_version": 3,
        "artifact_id": _REPORT_PATH,
        "as_of_date": selected.verified_data_as_of_date.isoformat(),
        "status": "VERIFIED",
        "verifier": verifier_artifact,
        "selected_source_identities": selected_source_identities,
        "subjects": subjects,
        "subjects_semantic_sha256": _selected_report_subjects_semantic_sha256(
            selected, subjects
        ),
        "counts": thawed["counts"],
        "blocker_chains": thawed["blocker_chains"],
        "numeric_verification": thawed["numeric_verification"],
        "invariants": thawed["invariants"],
    }


def _canonical_independent_report_bytes(report: Mapping[str, object]) -> bytes:
    """Canonicalize only an ordinary JSON-safe report tree."""

    active_containers: set[int] = set()

    def validate_tree(value: object) -> None:
        if type(value) is dict:
            identity = id(value)
            if identity in active_containers:
                _fail()
            active_containers.add(identity)
            for key, item in value.items():
                if type(key) is not str:
                    _fail()
                validate_tree(item)
            active_containers.remove(identity)
            return
        if type(value) is list:
            identity = id(value)
            if identity in active_containers:
                _fail()
            active_containers.add(identity)
            for item in value:
                validate_tree(item)
            active_containers.remove(identity)
            return
        if type(value) in {str, int, bool, type(None)}:
            return
        _fail()

    if type(report) is not dict:
        _fail()
    validate_tree(report)
    try:
        return canonical_json_bytes(report)
    except (TypeError, ValueError):
        _fail()


def _verify_c4b2_policy_and_flows(
    selected: IndependentVerifierInputs,
    subject_payloads: Mapping[str, bytes],
    *,
    enforce_global_accounting_currency: bool = True,
) -> None:
    exact, prefixes, policy = _classification_rules(selected.flow_classification_policy)
    policy_projection = _strict_object(
        subject_payloads["authority/flow-classification-policy.json"]
    )
    if policy_projection != policy:
        _fail()
    ledger = _ledger_with_derived(selected)
    raw_flows = ledger.get("flows")
    if not isinstance(raw_flows, list):
        _fail()
    raw_by_id: dict[str, dict[str, object]] = {}
    _, accounting_currency = _normalize_provider_navs(
        selected,
        enforce_global_accounting_currency=enforce_global_accounting_currency,
    )
    for flow in raw_flows:
        if not isinstance(flow, dict):
            _fail()
        record_id = flow.get("record_id")
        account_id = _internal_id(flow.get("account_id"))
        event_date = _date_value(flow.get("event_date"))
        nav_recognition_date = _date_value(
            flow.get("nav_recognition_date", flow.get("event_date"))
        )
        flow_type = flow.get("flow_type")
        amount = flow.get("amount")
        if (
            not isinstance(record_id, str)
            or not record_id
            or record_id in raw_by_id
            or not isinstance(flow_type, str)
            or not flow_type
            or not isinstance(amount, dict)
            or set(amount) != {"currency", "status", "value"}
        ):
            _fail()
        currency = amount.get("currency")
        status = amount.get("status")
        currency_pattern = (
            r"[A-Z]{3}" if enforce_global_accounting_currency else r"[A-Z]{3,12}"
        )
        if not isinstance(currency, str) or not re.fullmatch(
            currency_pattern, currency
        ):
            _fail()
        if status == "AVAILABLE":
            value = amount.get("value")
            decimal_value = _parse_retained_source_decimal(value)
            if enforce_global_accounting_currency and currency != accounting_currency:
                _fail()
        elif status == "UNAVAILABLE":
            if amount.get("value") is not None:
                _fail()
            decimal_value = None
        else:
            _fail()
        raw_by_id[record_id] = {
            "record_id": record_id,
            "account_id": account_id,
            "event_date": event_date.isoformat(),
            "nav_recognition_date": nav_recognition_date.isoformat(),
            "flow_type": flow_type,
            "amount": dict(amount),
            "decimal_value": decimal_value,
        }
        raw_by_id[record_id].update(_retained_flow_scope_metadata(flow))
        provider_id = flow.get("provider_id")
        if provider_id is not None:
            if not isinstance(provider_id, str) or not provider_id.strip():
                _fail()
            raw_by_id[record_id]["provider_id"] = provider_id.strip()
        generation_id = flow.get("generation_id")
        if generation_id is not None:
            if (
                not isinstance(generation_id, str)
                or _SHA256.fullmatch(generation_id) is None
            ):
                _fail()
            raw_by_id[record_id]["generation_id"] = generation_id
        activity_id = flow.get("provider_activity_id_sha256")
        if activity_id is not None:
            if (
                not isinstance(activity_id, str)
                or _SHA256.fullmatch(activity_id) is None
            ):
                _fail()
            if (
                "provider_id" not in raw_by_id[record_id]
                or "generation_id" not in raw_by_id[record_id]
            ):
                _fail()
            raw_by_id[record_id]["provider_activity_id_sha256"] = activity_id

    def select_rule(flow_type: str) -> dict[str, str] | None:
        rule = exact.get(flow_type)
        if rule is not None:
            return rule
        matches = [
            rule for rule in prefixes if flow_type.startswith(rule["flow_type_prefix"])
        ]
        if not matches:
            return None
        if len(matches) != 1:
            _fail()
        return matches[0]

    selected_rules = {
        record_id: select_rule(str(flow["flow_type"]))
        for record_id, flow in raw_by_id.items()
    }

    def identity_compatible(
        flow: Mapping[str, object], other: Mapping[str, object]
    ) -> bool:
        rule = selected_rules[str(flow["record_id"])]
        if (
            rule is None
            or rule.get("counterparty_rule") != _SOURCE_IDENTITY_COUNTERPARTY_RULE
        ):
            return True
        activity_id = flow.get("provider_activity_id_sha256")
        other_activity_id = other.get("provider_activity_id_sha256")
        if activity_id is None and other_activity_id is None:
            return True
        return activity_id is not None and (
            flow.get("provider_id"),
            flow.get("generation_id"),
            activity_id,
        ) == (
            other.get("provider_id"),
            other.get("generation_id"),
            other_activity_id,
        )

    physical_candidates: dict[str, list[str]] = {}
    for record_id, flow in raw_by_id.items():
        if flow["amount"]["status"] != "AVAILABLE":
            physical_candidates[record_id] = []
            continue
        physical_candidates[record_id] = [
            other_id
            for other_id, other in raw_by_id.items()
            if other_id != record_id
            and other["account_id"] != flow["account_id"]
            and other["event_date"] == flow["event_date"]
            and other["amount"]["status"] == "AVAILABLE"
            and other["amount"]["currency"] == flow["amount"]["currency"]
            and other["decimal_value"] == -flow["decimal_value"]
            and identity_compatible(flow, other)
        ]

    base_expected_events: list[dict[str, object]] = []
    for record_id in sorted(raw_by_id):
        flow = raw_by_id[record_id]
        rule = selected_rules[record_id]
        if rule is None:
            event = {
                "event_id": f"classified-flow:{record_id}",
                "source_record_id": record_id,
                "account_id": flow["account_id"],
                "event_date": flow["event_date"],
                "nav_recognition_date": flow["nav_recognition_date"],
                "flow_type": flow["flow_type"],
                "amount": flow["amount"],
                "classification_status": "BLOCKED",
                "disposition": None,
                "blocker_code": "FLOW_TYPE_UNRESOLVED",
            }
        elif flow["amount"]["status"] == "UNAVAILABLE":
            event: dict[str, object] = {
                "event_id": f"classified-flow:{record_id}",
                "source_record_id": record_id,
                "account_id": flow["account_id"],
                "event_date": flow["event_date"],
                "nav_recognition_date": flow["nav_recognition_date"],
                "flow_type": flow["flow_type"],
                "amount": flow["amount"],
                "classification_status": "BLOCKED",
                "disposition": None,
                "blocker_code": "AMOUNT_UNAVAILABLE",
            }
        else:
            sign = rule["amount_sign"]
            value = flow["decimal_value"]
            if (
                (sign == "POSITIVE" and value <= 0)
                or (sign == "NEGATIVE" and value >= 0)
                or (sign == "NONZERO" and value == 0)
            ):
                event = {
                    "event_id": f"classified-flow:{record_id}",
                    "source_record_id": record_id,
                    "account_id": flow["account_id"],
                    "event_date": flow["event_date"],
                    "nav_recognition_date": flow["nav_recognition_date"],
                    "flow_type": flow["flow_type"],
                    "amount": flow["amount"],
                    "classification_status": "BLOCKED",
                    "disposition": None,
                    "blocker_code": "FLOW_AMOUNT_SIGN_MISMATCH",
                }
            else:
                event = {
                    "event_id": f"classified-flow:{record_id}",
                    "source_record_id": record_id,
                    "account_id": flow["account_id"],
                    "event_date": flow["event_date"],
                    "nav_recognition_date": flow["nav_recognition_date"],
                    "flow_type": flow["flow_type"],
                    "amount": flow["amount"],
                    "classification_status": "CLASSIFIED",
                    "disposition": rule["disposition"],
                    "blocker_code": None,
                }
        base_expected_events.append(event)
    base_events_by_source_id = {
        str(event["source_record_id"]): event for event in base_expected_events
    }
    expected_events: list[dict[str, object]] = []
    for base_event in base_expected_events:
        event = dict(base_event)
        if (
            event["classification_status"] == "CLASSIFIED"
            and event["disposition"] == "INTERNAL"
        ):
            record_id = str(event["source_record_id"])
            candidates = physical_candidates[record_id]
            blocker_code: str | None = None
            if not candidates:
                fallback = selected_rules[record_id].get("unpaired_disposition")
                if fallback is None:
                    blocker_code = "INTERNAL_COUNTERPARTY_MISSING"
                else:
                    event["disposition"] = fallback
            elif len(candidates) > 1:
                blocker_code = "INTERNAL_COUNTERPARTY_AMBIGUOUS"
            else:
                counterparty_id = candidates[0]
                counterpart = base_events_by_source_id[counterparty_id]
                counterpart_rule = selected_rules[counterparty_id]
                if not (
                    counterpart["classification_status"] == "CLASSIFIED"
                    and counterpart["disposition"] == "INTERNAL"
                    and counterpart_rule is not None
                    and counterpart_rule.get("counterparty_rule")
                    == selected_rules[record_id].get("counterparty_rule")
                    and physical_candidates[counterparty_id] == [record_id]
                ):
                    blocker_code = "INTERNAL_COUNTERPARTY_CLASSIFICATION_BLOCKED"
                else:
                    event["counterparty_source_record_id"] = counterparty_id
            if blocker_code is not None:
                event["classification_status"] = "BLOCKED"
                event["disposition"] = None
                event["blocker_code"] = blocker_code
        expected_events.append(event)
    classified = _strict_object(subject_payloads["authority/classified-flows.json"])
    expected_policy_reference = {
        "policy_id": policy["policy_id"],
        "policy_version": policy["policy_version"],
        "policy_as_of_date": policy["as_of_date"],
        "policy_sha256": hashlib.sha256(canonical_json_bytes(policy)).hexdigest(),
    }
    selected_identity = _selected_identity(selected)
    current_product = selected.current_epoch_crosswalk is not None
    if not current_product:
        for event in expected_events:
            event.pop("nav_recognition_date")
    expected_classified = {
        "schema_version": 5 if current_product else 4,
        "artifact_id": "authority/classified-flows.json",
        "as_of_date": selected.generation_as_of_date.isoformat(),
        "ledger_identity": {
            "generation_id": selected_identity["provider_ledger"]["generation_id"],
            "artifact": selected_identity["provider_ledger"]["artifact"],
        },
        "classification_policy": expected_policy_reference,
        "events": expected_events,
    }
    if current_product:
        capital_anchors = ledger.get("capital_anchors")
        if not isinstance(capital_anchors, list):
            _fail()
        expected_classified["capital_anchors"] = capital_anchors
    if classified != expected_classified:
        _fail()


def _canonical_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_base64(value: object) -> bytes:
    if not isinstance(value, str) or not value.isascii():
        _fail()
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        _fail()
    if _canonical_base64(decoded) != value:
        _fail()
    return decoded


def _transport_object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        _fail()
    return value


def encode_verification_request(
    selected_snapshots: IndependentVerifierInputs,
    subject_payloads: Mapping[object, object],
) -> bytes:
    """Encode only source/subject bytes in the version-1 canonical transport."""

    if type(selected_snapshots) is not IndependentVerifierInputs:
        _fail()
    layout, normalized_subjects = _normalize_selected_layout_and_subjects(
        selected_snapshots, subject_payloads
    )
    if layout == "mixed" and selected_snapshots.current_epoch_crosswalk is None:
        _fail()
    selected = {
        f"{role}_base64": _canonical_base64(snapshot.contents)
        for role, snapshot in zip(
            _SNAPSHOT_ROLES, _snapshot_roles(selected_snapshots), strict=True
        )
    }
    if layout == "mixed":
        selected.update(
            {
                f"{role}_base64": _canonical_base64(snapshot.contents)
                for role, snapshot in zip(
                    _MIXED_SNAPSHOT_ROLES,
                    _mixed_snapshot_roles(selected_snapshots),
                    strict=True,
                )
            }
        )
        assert selected_snapshots.current_epoch_crosswalk is not None
        selected["current_epoch_crosswalk_base64"] = _canonical_base64(
            selected_snapshots.current_epoch_crosswalk.contents
        )
    receipt_items = (
        selected_snapshots.retrieval_receipts.items()
        if layout == "legacy"
        else (
            (receipt_id, selected_snapshots.retrieval_receipts[receipt_id])
            for receipt_id in sorted(
                selected_snapshots.retrieval_receipts, key=str.casefold
            )
        )
    )
    receipts = [
        {
            "receipt_id": receipt_id,
            "contents_base64": _canonical_base64(snapshot.contents),
        }
        for receipt_id, snapshot in receipt_items
    ]
    subject_paths = (
        EXACT_SUBJECT_PATHS
        if layout == "legacy"
        else _mixed_subject_paths(selected_snapshots)
    )
    subjects = [
        {"path": path, "contents_base64": _canonical_base64(normalized_subjects[path])}
        for path in subject_paths
    ]
    request: dict[str, object] = {
        "schema_version": _REQUEST_VERSION
        if layout == "legacy"
        else _CROSSWALK_REQUEST_VERSION,
        "selected_snapshots": selected,
        "retrieval_receipts": receipts,
        "subjects": subjects,
    }
    if selected_snapshots.sleeve_facts:
        if layout != "mixed":
            _fail()
        request["sleeve_facts"] = [
            {"contents_base64": _canonical_base64(snapshot.contents)}
            for snapshot in sorted(
                selected_snapshots.sleeve_facts, key=lambda item: item.sha256
            )
        ]
    return canonical_json_bytes(request)


def decode_verification_request(
    request_bytes: bytes,
) -> tuple[IndependentVerifierInputs, dict[str, bytes]]:
    """Decode and validate one supported canonical request transport."""

    if type(request_bytes) is not bytes:
        _fail()
    try:
        parsed = strict_json_loads(request_bytes, label="verification request")
    except ValueError:
        _fail()
    if canonical_json_bytes(parsed) != request_bytes:  # type: ignore[arg-type]
        _fail()
    root = _transport_object(parsed)
    required_root = {
        "schema_version",
        "selected_snapshots",
        "retrieval_receipts",
        "subjects",
    }
    if set(root) != required_root and set(root) != required_root | {"sleeve_facts"}:
        _fail()
    request_version = root["schema_version"]
    if isinstance(request_version, bool) or request_version not in {
        _REQUEST_VERSION,
        2,
        _CROSSWALK_REQUEST_VERSION,
    }:
        _fail()

    selected = _transport_object(root["selected_snapshots"])
    expected_selected_keys = {f"{role}_base64" for role in _SNAPSHOT_ROLES}
    if request_version in {2, _CROSSWALK_REQUEST_VERSION}:
        expected_selected_keys |= {f"{role}_base64" for role in _MIXED_SNAPSHOT_ROLES}
    if request_version == _CROSSWALK_REQUEST_VERSION:
        expected_selected_keys.add("current_epoch_crosswalk_base64")
    if set(selected) != expected_selected_keys:
        _fail()
    decoded_selected = {
        role: _decode_base64(selected[f"{role}_base64"])
        for role in (
            _SNAPSHOT_ROLES
            if request_version == 1
            else (*_SNAPSHOT_ROLES, *_MIXED_SNAPSHOT_ROLES)
        )
    }

    raw_receipts = root["retrieval_receipts"]
    if not isinstance(raw_receipts, list):
        _fail()
    receipts: dict[str, InputSnapshot] = {}
    prior_receipt_id: str | None = None
    for raw_entry in raw_receipts:
        entry = _transport_object(raw_entry)
        if set(entry) != {"receipt_id", "contents_base64"}:
            _fail()
        receipt_id = _portable_identifier(entry["receipt_id"])
        if prior_receipt_id is not None:
            if request_version == 1 and receipt_id <= prior_receipt_id:
                _fail()
            if (
                request_version in {2, _CROSSWALK_REQUEST_VERSION}
                and receipt_id.casefold() <= prior_receipt_id.casefold()
            ):
                _fail()
        prior_receipt_id = receipt_id
        receipts[receipt_id] = InputSnapshot.from_bytes(
            "retrieval receipt", _decode_base64(entry["contents_base64"])
        )

    kwargs = {
        role: InputSnapshot.from_bytes(role.replace("_", " "), decoded_selected[role])
        for role in _SNAPSHOT_ROLES
    }
    if request_version in {2, _CROSSWALK_REQUEST_VERSION}:
        kwargs.update(
            {
                role: InputSnapshot.from_bytes(
                    role.replace("_", " "), decoded_selected[role]
                )
                for role in _MIXED_SNAPSHOT_ROLES
            }
        )
    if request_version == _CROSSWALK_REQUEST_VERSION:
        kwargs["current_epoch_crosswalk"] = InputSnapshot.from_bytes(
            "current epoch crosswalk",
            _decode_base64(selected["current_epoch_crosswalk_base64"]),
        )
    if "sleeve_facts" in root:
        if request_version != _CROSSWALK_REQUEST_VERSION:
            _fail()
        raw_facts = root["sleeve_facts"]
        if not isinstance(raw_facts, list) or not raw_facts:
            _fail()
        decoded_facts: list[InputSnapshot] = []
        for raw_entry in raw_facts:
            entry = _transport_object(raw_entry)
            if set(entry) != {"contents_base64"}:
                _fail()
            decoded_facts.append(
                InputSnapshot.from_bytes(
                    "sleeve facts", _decode_base64(entry["contents_base64"])
                )
            )
        if [item.sha256 for item in decoded_facts] != sorted(
            item.sha256 for item in decoded_facts
        ):
            _fail()
        kwargs["sleeve_facts"] = tuple(decoded_facts)
    selected_inputs = IndependentVerifierInputs(
        **kwargs,
        retrieval_receipts=receipts,
    )
    expected_subject_paths = (
        EXACT_SUBJECT_PATHS
        if request_version == 1
        else _mixed_subject_paths(selected_inputs)
    )
    expected_subject_set = frozenset(expected_subject_paths)

    raw_subjects = root["subjects"]
    if not isinstance(raw_subjects, list):
        _fail()
    subjects: dict[str, bytes] = {}
    prior_path: str | None = None
    for index, raw_entry in enumerate(raw_subjects):
        entry = _transport_object(raw_entry)
        if set(entry) != {"path", "contents_base64"}:
            _fail()
        path = entry["path"]
        if not isinstance(path, str) or path not in expected_subject_set:
            _fail()
        if request_version == 1:
            if prior_path is not None and path <= prior_path:
                _fail()
        elif (
            index >= len(expected_subject_paths)
            or path != expected_subject_paths[index]
        ):
            _fail()
        prior_path = path
        subjects[path] = _decode_base64(entry["contents_base64"])
    if request_version in {2, _CROSSWALK_REQUEST_VERSION} and len(raw_subjects) != len(
        expected_subject_paths
    ):
        _fail()
    subjects = (
        _normalize_subject_payloads(subjects)
        if request_version == 1
        else _normalize_subject_payloads_for_paths(subjects, expected_subject_paths)
    )
    return (
        selected_inputs,
        subjects,
    )


class IndependentAccountManifestVerifier:
    """Independently derive retained C4 semantics and canonical report bytes."""

    def verify(
        self,
        selected_snapshots: IndependentVerifierInputs,
        subject_payloads: Mapping[object, object],
    ) -> bytes:
        if type(selected_snapshots) is not IndependentVerifierInputs:
            _fail()
        if selected_snapshots._is_mixed:
            _validate_approved_manifest_schema(
                _require_snapshot(selected_snapshots.manifest_schema)
            )
        layout, normalized = _normalize_selected_layout_and_subjects(
            selected_snapshots, subject_payloads
        )
        state = (
            _verify_retained_semantics_normalized(selected_snapshots, normalized)
            if layout == "legacy"
            else _verify_mixed_semantics_normalized(selected_snapshots, normalized)
        )
        numeric = state["numeric_verification"]
        if type(numeric) is not dict:
            _fail()
        try:
            comparison = _NumericComparison(
                numeric["candidate_count"],
                numeric["comparison_count"],
                numeric["discrepancy_count"],
            )
        except (KeyError, TypeError):
            _fail()
        _validate_reportable_comparison(
            comparison,
            _context=(
                _D2A_REPORTABILITY_CONTEXT
                if layout == "legacy"
                else _MIXED_REPORTABILITY_CONTEXT
            ),
        )
        if layout == "legacy":
            _validate_numeric_decision(comparison, numeric.get("disposition"), state)
        else:
            _validate_mixed_candidate_state(state)
        retained_candidate = _build_retained_report_candidate(state)
        report = (
            _assemble_independent_report(
                selected_snapshots, normalized, retained_candidate
            )
            if layout == "legacy"
            else _assemble_mixed_report(
                selected_snapshots, normalized, retained_candidate
            )
        )
        return _canonical_independent_report_bytes(report)

    def verify_finalized_report(
        self,
        selected_snapshots: IndependentVerifierInputs,
        subject_payloads: Mapping[object, object],
        finalized_report: bytes,
    ) -> None:
        if type(finalized_report) is not bytes:
            _fail()
        expected = IndependentAccountManifestVerifier.verify(
            self, selected_snapshots, subject_payloads
        )
        if finalized_report != expected:
            _fail()


def _cli_parse_argv(argv: object) -> tuple[str, str | None]:
    """Accept only the two literal D3c command forms."""

    if type(argv) is not tuple:
        _fail()
    if len(argv) == 2:
        request_flag, request_path = argv
        finalized_path = None
    elif len(argv) == 4:
        request_flag, request_path, finalized_flag, finalized_path = argv
        if finalized_flag != "--finalized-report":
            _fail()
    else:
        _fail()
    if request_flag != "--request":
        _fail()
    if type(request_path) is not str or not request_path:
        _fail()
    if finalized_path is not None and (
        type(finalized_path) is not str or not finalized_path
    ):
        _fail()
    return request_path, finalized_path


def _cli_prepare(
    argv: object,
) -> tuple[IndependentVerifierInputs, dict[str, bytes], object | None]:
    """Decode one request and attest the executing verifier source bytes."""

    from pathlib import Path

    request_name, finalized_name = _cli_parse_argv(argv)
    selected, subjects = decode_verification_request(Path(request_name).read_bytes())
    executing_path = Path(__file__)
    if executing_path.is_symlink():
        _fail()
    source_bytes = executing_path.resolve(strict=True).read_bytes()
    if source_bytes != selected.independent_verifier_source.contents:
        _fail()
    return selected, subjects, None if finalized_name is None else Path(finalized_name)


def _cli_dispatch(argv: object) -> bytes | None:
    """Run only the C2a request-mode verifier path."""

    selected, subjects, finalized_path = _cli_prepare(argv)
    if finalized_path is not None:
        raise IndependentVerificationError(
            "independent verification input is invalid"
        ) from None
    verifier = IndependentAccountManifestVerifier()
    return verifier.verify(selected, subjects)


def _cli_main() -> int:
    """Contain expected CLI operational failures at one generic boundary."""

    import sys

    try:
        result = _cli_dispatch(tuple(sys.argv[1:]))
        if result is not None:
            written = sys.stdout.buffer.write(result)
            if written != len(result):
                raise IndependentVerificationError(
                    "independent verification input is invalid"
                ) from None
        return 0
    except Exception:
        try:
            sys.stderr.buffer.write(b"INDEPENDENT_VERIFICATION_FAILED\n")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())


__all__ = [
    "EXACT_SUBJECT_PATHS",
    "IndependentAccountManifestVerifier",
    "IndependentVerifierInputs",
    "IndependentVerificationError",
    "canonical_subject_entries",
    "decode_verification_request",
    "encode_verification_request",
    "subjects_semantic_sha256",
]
