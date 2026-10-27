"""Build the pure, hash-bound account-assignment perimeter artifact."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import MappingProxyType
from typing import Mapping, Sequence

from quant_engine.core import (
    GenerationBundle,
    GenerationFileIdentity,
    InputSnapshot,
    read_generation_bundle,
    stage_generation_bundle,
)
from quant_engine.core.contracts import (
    AdapterIdentity,
    CoverageClaim,
    CoverageStatus,
    GenerationManifest,
    ManifestValidationError,
    SourceArtifact,
    VerificationStatus,
    canonical_json_bytes,
)
from tools.account_strategy_manifest import (
    PERFORMANCE_ROW_FIELDS,
    PERIOD_RETURN_FIELDS,
    ManifestCandidateSnapshots,
    ManifestError,
    SleeveManifestFacts,
    apply_sleeve_assignments,
    derive_sleeve_manifest_facts,
    build_manifest_candidate_from_snapshots,
    json_ready,
    load_flow_classification_policy,
    load_flow_coverage,
    partition_assignments,
    validate_manifest_schema,
)


_ARTIFACT_ID = "account-assignment-perimeter"
_BASELINE_ARTIFACT_ID = "normalized/portfolio-baseline.json"
_BASELINE_SCHEMA_NAME = "portfolio-baseline-catalog"
_PROVIDER_LEDGER_ARTIFACT_ID = "normalized/provider-ledger.json"
_PROVIDER_LEDGER_SCHEMA_NAME = "provider-ledger-catalog"
_GENERATION_MANIFEST_SCHEMA_NAME = "generation-manifest"
_GENERATION_MANIFEST_SCHEMA_VERSION = "1"
_CODE_SOURCE_ARTIFACT_IDS = {
    "epoch_producer": "tools/account_strategy_manifest.py",
    "generation_builder": "tools/build_account_manifest_generation.py",
    "independent_verifier": "tools/verify_account_manifest_generation.py",
}
_DISPOSITIONS = (
    "STRATEGY_MAPPED",
    "HISTORICAL_POOL_ONLY",
    "RESERVE_NOT_APPLICABLE",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_IDENTITY_COUNTERPARTY_RULE = (
    "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT_WITH_SOURCE_IDENTITY_V1"
)
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_INTERNAL_ACCOUNT_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CROSSWALK_DISPOSITIONS = frozenset(
    {
        "INCLUDED_PRODUCT_EPOCH",
        "EXCLUDED_DEPRECATED_HISTORY_ONLY",
        "EXCLUDED_IMMATERIAL",
    }
)
_CANONICAL_DECIMAL = re.compile(r"^-?(0|[1-9][0-9]*)(?:\.[0-9]+)?$")
DERIVED_SLEEVE_COVERAGE_KIND = "DERIVED_SLEEVE_COVERAGE_V1"
_NINE_BLOCKED_PATHS = (
    "authority/selected-input-identities.json",
    "authority/account-assignment-perimeter.json",
    "authority/calculation-inputs.json",
    "authority/flow-classification-policy.json",
    "authority/classified-flows.json",
    "authority/account-flow-coverage.json",
    "authority/mapped-epoch-admission.json",
    "authority/code-identities.json",
    "outputs/account-strategy-manifest.json",
)
_CROSSWALK_SUBJECT_PATH = "authority/account-assignment-crosswalk.json"
_INDEPENDENT_REPORT_PATH = "verification/independent-verification.json"
_GENERATION_ADAPTER = AdapterIdentity("account-manifest-generation-builder", "1")
_GENERATION_COVERAGE_BOUNDARY = (
    "Exact account-manifest subject and independent-verification artifact bytes only; "
    "row availability is encoded in the selected output artifact."
)
_SUBJECT_SCHEMAS = MappingProxyType(
    {
        "authority/account-assignment-perimeter.json": (
            "account-assignment-perimeter",
            "1",
        ),
        "authority/account-flow-coverage.json": ("account-flow-coverage", "2"),
        "authority/calculation-inputs.json": (
            "account-manifest-calculation-inputs",
            "4",
        ),
        "authority/classified-flows.json": ("classified-flows", "5"),
        "authority/code-identities.json": (
            "account-manifest-code-identities",
            "1",
        ),
        "authority/flow-classification-policy.json": (
            "flow-classification-policy",
            "1",
        ),
        "authority/mapped-epoch-admission.json": (
            "account-strategy-manifest-admission-policy",
            "2",
        ),
        "authority/selected-input-identities.json": (
            "selected-input-identities",
            "1",
        ),
        _CROSSWALK_SUBJECT_PATH: ("account-strategy-current-epoch-crosswalk", "1"),
        "outputs/account-strategy-manifest.json": (
            "account-strategy-manifest",
            "10",
        ),
        "authority/account-interval-retrieval-receipt-authority.json": (
            "account-interval-retrieval-receipt-authority",
            "1",
        ),
    }
)


class AccountAssignmentPerimeterError(ValueError):
    """The input boundary or proposed perimeter payload is invalid."""


def _validate_current_epoch_crosswalk(
    baseline: InputSnapshot,
    crosswalk: InputSnapshot,
    *,
    baseline_generation_id: str | None = None,
) -> list[dict[str, object]]:
    """Bind the approved 15-row crosswalk to its exact mapped baseline rows."""

    try:
        baseline_payload = baseline.json_object("selected baseline")
        payload = crosswalk.json_object("current epoch crosswalk")
    except ValueError as exc:
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk is invalid"
        ) from exc
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "artifact_id",
        "as_of_date",
        "calculation_cutoff_date",
        "disposition_counts",
        "selected_baseline",
        "rows",
    }:
        raise AccountAssignmentPerimeterError("current epoch crosswalk is invalid")
    binding = payload["selected_baseline"]
    if not isinstance(binding, Mapping) or binding != {
        "generation_id": baseline_generation_id
        if baseline_generation_id is not None
        else binding.get("generation_id"),
        "artifact_id": _BASELINE_ARTIFACT_ID,
        "sha256": baseline.sha256,
        "size_bytes": baseline.size_bytes,
        "as_of_date": baseline_payload.get("as_of_date"),
        "schema_name": _BASELINE_SCHEMA_NAME,
        "schema_version": str(baseline_payload.get("baseline_schema_version")),
    }:
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk baseline binding is invalid"
        )
    if (
        payload["schema_version"] != 1
        or payload["artifact_id"] != "account-strategy-current-epoch-crosswalk"
    ):
        raise AccountAssignmentPerimeterError("current epoch crosswalk is invalid")
    if payload["as_of_date"] != baseline_payload.get("as_of_date"):
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk dates are invalid"
        )
    try:
        if date.fromisoformat(
            str(payload["calculation_cutoff_date"])
        ) > date.fromisoformat(str(payload["as_of_date"])):
            raise ValueError
    except ValueError as exc:
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk dates are invalid"
        ) from exc
    accounts = baseline_payload.get("accounts")
    assignments = baseline_payload.get("assignments")
    rows = payload.get("rows")
    if (
        not isinstance(accounts, list)
        or not isinstance(assignments, list)
        or not isinstance(rows, list)
    ):
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk rows are invalid"
        )
    providers = {
        item.get("account_id"): item.get("provider")
        for item in accounts
        if isinstance(item, Mapping)
    }
    mapped = {
        item.get("assignment_id"): item
        for item in assignments
        if isinstance(item, Mapping)
        and item.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
    }
    if len(mapped) != 15 or len(rows) != 15 or len(set(mapped)) != 15:
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk rows are invalid"
        )
    by_assignment: dict[str, Mapping[str, object]] = {}
    composite_keys: set[tuple[object, object, object, object]] = set()
    labels: dict[str, tuple[object, object, object]] = {}
    included_intervals: dict[str, list[tuple[date, date | None]]] = {}
    included: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
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
            raise AccountAssignmentPerimeterError(
                "current epoch crosswalk row is invalid"
            )
        assignment_id = row.get("assignment_id")
        if (
            not isinstance(assignment_id, str)
            or assignment_id in by_assignment
            or assignment_id not in mapped
        ):
            raise AccountAssignmentPerimeterError(
                "current epoch crosswalk assignment is invalid"
            )
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
            raise AccountAssignmentPerimeterError(
                "current epoch crosswalk row differs from baseline"
            )
        composite_key = tuple(
            row.get(key)
            for key in ("account_id", "strategy_id", "effective_start", "effective_end")
        )
        if composite_key in composite_keys:
            raise AccountAssignmentPerimeterError(
                "current epoch crosswalk epoch keys are not unique"
            )
        composite_keys.add(composite_key)
        disposition = row.get("product_epoch_disposition")
        if (
            disposition not in _CROSSWALK_DISPOSITIONS
            or not isinstance(row.get("account_label"), str)
            or not row["account_label"]
            or (
                row.get("last_four") is not None
                and (
                    not isinstance(row.get("last_four"), str)
                    or re.fullmatch(r"[0-9]{4}", row["last_four"]) is None
                )
            )
        ):
            raise AccountAssignmentPerimeterError(
                "current epoch crosswalk row is invalid"
            )
        account_id = str(row["account_id"])
        label = (row["provider"], row["account_label"], row["last_four"])
        if account_id in labels and labels[account_id] != label:
            raise AccountAssignmentPerimeterError(
                "current epoch crosswalk labels are inconsistent"
            )
        labels[account_id] = label
        try:
            start = date.fromisoformat(str(row["effective_start"]))
            end = (
                None
                if row["effective_end"] is None
                else date.fromisoformat(str(row["effective_end"]))
            )
        except ValueError as exc:
            raise AccountAssignmentPerimeterError(
                "current epoch crosswalk dates are invalid"
            ) from exc
        if end is not None and end < start:
            raise AccountAssignmentPerimeterError(
                "current epoch crosswalk dates are invalid"
            )
        if disposition == "INCLUDED_PRODUCT_EPOCH":
            included_intervals.setdefault(account_id, []).append((start, end))
        by_assignment[assignment_id] = row
    counts = payload.get("disposition_counts")
    actual_counts = Counter(
        row["product_epoch_disposition"] for row in by_assignment.values()
    )
    if (
        set(by_assignment) != set(mapped)
        or not isinstance(counts, Mapping)
        or set(counts) != _CROSSWALK_DISPOSITIONS
        or any(type(value) is not int or value < 0 for value in counts.values())
        or {key: actual_counts[key] for key in _CROSSWALK_DISPOSITIONS} != dict(counts)
    ):
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk dispositions are invalid"
        )
    for intervals in included_intervals.values():
        intervals.sort(key=lambda interval: interval[0])
        for (_, previous_end), (current_start, _) in zip(intervals, intervals[1:]):
            if previous_end is None or current_start <= previous_end:
                raise AccountAssignmentPerimeterError(
                    "current epoch crosswalk included epochs overlap"
                )
    for row in by_assignment.values():
        if row["product_epoch_disposition"] == "INCLUDED_PRODUCT_EPOCH":
            included.append(dict(mapped[str(row["assignment_id"])]))
    if len(included) != 13:
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk product counts are invalid"
        )
    included.sort(key=lambda row: str(row["assignment_id"]))
    return included


def _current_epoch_calculation_cutoff(
    selected_inputs: "AccountManifestSelectedInputs",
) -> date | None:
    if selected_inputs.current_epoch_crosswalk is None:
        return None
    _validate_current_epoch_crosswalk(
        selected_inputs.baseline,
        selected_inputs.current_epoch_crosswalk,
        baseline_generation_id=selected_inputs.baseline_generation_id,
    )
    payload = selected_inputs.current_epoch_crosswalk.json_object(
        "current epoch crosswalk"
    )
    return date.fromisoformat(str(payload["calculation_cutoff_date"]))


_LIFECYCLE_SCHEMA_VERSION = 1


def _crosswalk_dispositions_by_epoch(
    crosswalk: InputSnapshot,
) -> dict[tuple[object, object, object, object], object]:
    """Return the owner crosswalk's product disposition keyed by epoch identity."""

    try:
        rows = crosswalk.json_object("current epoch crosswalk")["rows"]
    except (KeyError, ValueError, TypeError) as exc:
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk is invalid"
        ) from exc
    if not isinstance(rows, list):
        raise AccountAssignmentPerimeterError("current epoch crosswalk is invalid")
    result: dict[tuple[object, object, object, object], object] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise AccountAssignmentPerimeterError("current epoch crosswalk is invalid")
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
            raise AccountAssignmentPerimeterError("current epoch crosswalk is invalid")
        result[key] = row.get("product_epoch_disposition")
    return result


def _epoch_lifecycle_fields(
    row: Mapping[str, object], disposition: object
) -> dict[str, object]:
    """Lifecycle of one manifest epoch from governed facts only (#592).

    CLOSED when the epoch's own return closed inside it (``performance_closed_on``),
    RETIRED when its assignment ended, INACTIVE when it is open but the crosswalk
    keeps it outside the current product, ACTIVE otherwise.  Sleeve links stay
    null: a sleeve account only exists once the sleeve pipeline supplies it.
    """

    end = row.get("effective_end")
    closed = row.get("performance_closed_on") if row.get("status") == "OK" else None
    if closed is not None:
        state = "CLOSED"
    elif end is not None:
        state = "RETIRED"
    elif disposition != "INCLUDED_PRODUCT_EPOCH":
        state = "INACTIVE"
    else:
        state = "ACTIVE"
    return {
        "lifecycle_state": state,
        "retired_on": end if state in {"RETIRED", "CLOSED"} else None,
        "closed_on": closed if state == "CLOSED" else None,
        "parent_assignment_id": None,
        "sleeve_label": None,
    }


def _crosswalk_display_by_epoch(
    crosswalk: InputSnapshot,
) -> dict[tuple[object, object, object, object], dict[str, object]]:
    """Return only owner-supplied display fields keyed by the epoch identity."""

    try:
        rows = crosswalk.json_object("current epoch crosswalk")["rows"]
    except (KeyError, ValueError, TypeError) as exc:
        raise AccountAssignmentPerimeterError(
            "current epoch crosswalk is invalid"
        ) from exc
    if not isinstance(rows, list):
        raise AccountAssignmentPerimeterError("current epoch crosswalk is invalid")
    result: dict[tuple[object, object, object, object], dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise AccountAssignmentPerimeterError("current epoch crosswalk is invalid")
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
            raise AccountAssignmentPerimeterError("current epoch crosswalk is invalid")
        result[key] = {
            "account_label": row.get("account_label"),
            "last_four": row.get("last_four"),
        }
    return result


def _require_snapshot(value: object, field_name: str) -> InputSnapshot:
    if type(value) is not InputSnapshot:
        raise AccountAssignmentPerimeterError(f"{field_name} must be an InputSnapshot")
    return value


def _selected_top_level_date(snapshot: InputSnapshot) -> date:
    try:
        payload = snapshot.json_object("selected input")
    except ValueError:
        raise AccountAssignmentPerimeterError(
            "selected input must contain strict JSON"
        ) from None
    value = payload.get("as_of_date")
    if not isinstance(value, str):
        raise AccountAssignmentPerimeterError(
            "selected input as_of_date must be a canonical ISO date"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise AccountAssignmentPerimeterError(
            "selected input as_of_date must be a canonical ISO date"
        ) from None
    if parsed.isoformat() != value:
        raise AccountAssignmentPerimeterError(
            "selected input as_of_date must be a canonical ISO date"
        )
    return parsed


def derive_generation_as_of_date(
    provider_ledger: InputSnapshot,
    baseline: InputSnapshot,
) -> date:
    """Return the later top-level date from the two frozen catalog snapshots."""
    ledger = _require_snapshot(provider_ledger, "provider_ledger")
    selected_baseline = _require_snapshot(baseline, "baseline")
    return max(
        _selected_top_level_date(ledger), _selected_top_level_date(selected_baseline)
    )


def _reject_provider_fact_after_ledger_date(
    fact_date: str | date, ledger_as_of_date: date
) -> None:
    if isinstance(fact_date, str):
        fact_date = date.fromisoformat(fact_date)
    if fact_date > ledger_as_of_date:
        raise AccountAssignmentPerimeterError(
            "provider fact is after selected provider ledger as_of_date"
        )


def _validate_provider_generation_dates(
    ledger: Mapping[str, object], ledger_as_of_date: date
) -> None:
    generations = ledger.get("provider_generations", [])
    if not isinstance(generations, list):
        raise AccountAssignmentPerimeterError(
            "provider ledger provider_generations must be a list"
        )
    for generation in generations:
        if not isinstance(generation, Mapping):
            raise AccountAssignmentPerimeterError(
                "provider ledger provider generation is invalid"
            )
        _iso_date(
            generation.get("generation_as_of_date"),
            "provider generation as_of_date",
        )
        # Generation provenance is retained source metadata, not a financial
        # observation. Its canonical date remains validated and is bound to
        # the selected generation identity at each receipt seam below.


def _nonnegative_schema_version(snapshot: InputSnapshot, field_name: str) -> str:
    try:
        payload = snapshot.json_object("selected input")
    except ValueError:
        raise AccountAssignmentPerimeterError(
            "selected input must contain strict JSON"
        ) from None
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AccountAssignmentPerimeterError(
            f"{field_name} must be a non-negative integer"
        )
    return str(value)


def _snapshot_identity(snapshot: InputSnapshot) -> dict[str, object]:
    return {"sha256": snapshot.sha256, "size_bytes": snapshot.size_bytes}


def _canonical_receipt_id(value: object) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise AccountAssignmentPerimeterError(
            "retrieval receipt ID must be a portable canonical artifact identifier"
        )
    try:
        GenerationFileIdentity(value, "0" * 64, 0)
    except (ManifestValidationError, TypeError, ValueError):
        raise AccountAssignmentPerimeterError(
            "retrieval receipt ID must be a portable canonical artifact identifier"
        ) from None
    return value


def _generation_manifest_identity(
    snapshot: InputSnapshot, *, artifact_id: str, as_of_date: date
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "as_of_date": as_of_date.isoformat(),
        "schema": {
            "name": _GENERATION_MANIFEST_SCHEMA_NAME,
            "version": _GENERATION_MANIFEST_SCHEMA_VERSION,
        },
        **_snapshot_identity(snapshot),
    }


def _bound_catalog_identity(
    *,
    catalog: InputSnapshot,
    manifest_snapshot: InputSnapshot,
    catalog_artifact_id: str,
    catalog_schema_name: str,
    catalog_schema_version_field: str,
    manifest_identity_artifact_id: str,
) -> tuple[str, dict[str, object]]:
    selected_catalog = _require_snapshot(catalog, "selected catalog")
    selected_manifest = _require_snapshot(manifest_snapshot, "selected manifest")
    source_date = _selected_top_level_date(selected_catalog)
    expected_schema_version = _nonnegative_schema_version(
        selected_catalog, catalog_schema_version_field
    )
    try:
        manifest = GenerationManifest.from_json_bytes(selected_manifest.contents)
    except (ManifestValidationError, ValueError, TypeError) as exc:
        raise AccountAssignmentPerimeterError("selected manifest is invalid") from exc
    if selected_manifest.contents != manifest.to_json_bytes():
        raise AccountAssignmentPerimeterError(
            "selected manifest must use canonical bytes"
        )
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise AccountAssignmentPerimeterError("selected manifest must be VERIFIED")
    if _SHA256.fullmatch(manifest.generation_id) is None:
        raise AccountAssignmentPerimeterError(
            "selected manifest generation_id is invalid"
        )
    matching = [
        artifact
        for artifact in manifest.artifacts
        if artifact.artifact_id == catalog_artifact_id
    ]
    if len(matching) != 1:
        raise AccountAssignmentPerimeterError(
            "selected manifest must bind exactly one catalog artifact"
        )
    artifact = matching[0]
    if (
        artifact.sha256 != selected_catalog.sha256
        or artifact.size_bytes != selected_catalog.size_bytes
        or artifact.as_of_date != source_date
        or artifact.schema_name != catalog_schema_name
        or artifact.schema_version != expected_schema_version
    ):
        raise AccountAssignmentPerimeterError(
            "selected manifest catalog artifact differs from frozen catalog"
        )
    return manifest.generation_id, {
        "generation_id": manifest.generation_id,
        "artifact": artifact.to_dict(),
        "generation_manifest": _generation_manifest_identity(
            selected_manifest,
            artifact_id=manifest_identity_artifact_id,
            as_of_date=manifest.as_of_date,
        ),
    }


@dataclass(frozen=True)
class AccountManifestSelectedInputs:
    """One immutable authority boundary for all account-manifest generated bytes."""

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
    provider_ledger_generation_id: str = field(init=False)
    baseline_generation_id: str = field(init=False)
    generation_as_of_date: date = field(init=False)

    def __post_init__(self) -> None:
        provider_ledger = _require_snapshot(self.provider_ledger, "provider_ledger")
        provider_ledger_manifest = _require_snapshot(
            self.provider_ledger_manifest, "provider_ledger_manifest"
        )
        baseline = _require_snapshot(self.baseline, "baseline")
        baseline_manifest = _require_snapshot(
            self.baseline_manifest, "baseline_manifest"
        )
        flow_policy = _require_snapshot(
            self.flow_classification_policy, "flow_classification_policy"
        )
        epoch_source = _require_snapshot(
            self.epoch_producer_source, "epoch_producer_source"
        )
        builder_source = _require_snapshot(
            self.generation_builder_source, "generation_builder_source"
        )
        verifier_source = _require_snapshot(
            self.independent_verifier_source, "independent_verifier_source"
        )
        crosswalk = (
            None
            if self.current_epoch_crosswalk is None
            else _require_snapshot(
                self.current_epoch_crosswalk, "current_epoch_crosswalk"
            )
        )
        optional_snapshots = (
            self.receipt_authority,
            self.admission_policy,
            self.manifest_schema,
            self.membership_census,
        )
        if any(value is None for value in optional_snapshots) and any(
            value is not None for value in optional_snapshots
        ):
            raise AccountAssignmentPerimeterError(
                "receipt_authority, admission_policy, manifest_schema, and "
                "membership_census must be selected together"
            )
        if all(value is not None for value in optional_snapshots):
            authority, admission, schema, census = optional_snapshots
            assert (
                authority is not None
                and admission is not None
                and schema is not None
                and census is not None
            )
            authority = _require_snapshot(authority, "receipt_authority")
            admission = _require_snapshot(admission, "admission_policy")
            schema = _require_snapshot(schema, "manifest_schema")
            census = _require_snapshot(census, "membership_census")
        else:
            authority = admission = schema = census = None
        if not isinstance(self.retrieval_receipts, Mapping):
            raise AccountAssignmentPerimeterError(
                "retrieval_receipts must be a mapping"
            )
        sleeve_facts = tuple(
            _require_snapshot(item, "sleeve_facts") for item in self.sleeve_facts
        )
        if sleeve_facts and authority is None:
            raise AccountAssignmentPerimeterError(
                "sleeve_facts require the mixed current-product selection"
            )
        object.__setattr__(self, "sleeve_facts", sleeve_facts)
        receipts: dict[str, InputSnapshot] = {}
        for receipt_id, receipt in self.retrieval_receipts.items():
            canonical_receipt_id = _canonical_receipt_id(receipt_id)
            if canonical_receipt_id.casefold() in {
                existing.casefold() for existing in receipts
            }:
                raise AccountAssignmentPerimeterError(
                    "retrieval receipt IDs must be unique case-insensitively"
                )
            receipts[canonical_receipt_id] = _require_snapshot(
                receipt, "retrieval receipt"
            )
        ledger_id, _ = _bound_catalog_identity(
            catalog=provider_ledger,
            manifest_snapshot=provider_ledger_manifest,
            catalog_artifact_id=_PROVIDER_LEDGER_ARTIFACT_ID,
            catalog_schema_name=_PROVIDER_LEDGER_SCHEMA_NAME,
            catalog_schema_version_field="catalog_schema_version",
            manifest_identity_artifact_id="identity/provider-ledger-generation.json",
        )
        baseline_id, _ = _bound_catalog_identity(
            catalog=baseline,
            manifest_snapshot=baseline_manifest,
            catalog_artifact_id=_BASELINE_ARTIFACT_ID,
            catalog_schema_name=_BASELINE_SCHEMA_NAME,
            catalog_schema_version_field="baseline_schema_version",
            manifest_identity_artifact_id="identity/portfolio-baseline-generation.json",
        )
        object.__setattr__(self, "provider_ledger", provider_ledger)
        object.__setattr__(self, "provider_ledger_manifest", provider_ledger_manifest)
        object.__setattr__(self, "baseline", baseline)
        object.__setattr__(self, "baseline_manifest", baseline_manifest)
        object.__setattr__(self, "flow_classification_policy", flow_policy)
        object.__setattr__(self, "retrieval_receipts", MappingProxyType(dict(receipts)))
        object.__setattr__(self, "epoch_producer_source", epoch_source)
        object.__setattr__(self, "generation_builder_source", builder_source)
        object.__setattr__(self, "independent_verifier_source", verifier_source)
        object.__setattr__(self, "current_epoch_crosswalk", crosswalk)
        object.__setattr__(self, "receipt_authority", authority)
        object.__setattr__(self, "admission_policy", admission)
        object.__setattr__(self, "manifest_schema", schema)
        object.__setattr__(self, "membership_census", census)
        object.__setattr__(self, "provider_ledger_generation_id", ledger_id)
        object.__setattr__(self, "baseline_generation_id", baseline_id)
        object.__setattr__(
            self,
            "generation_as_of_date",
            derive_generation_as_of_date(provider_ledger, baseline),
        )


def _selected_input_identities(
    selected_inputs: AccountManifestSelectedInputs,
) -> dict[str, object]:
    if type(selected_inputs) is not AccountManifestSelectedInputs:
        raise AccountAssignmentPerimeterError(
            "selected_inputs must be an AccountManifestSelectedInputs"
        )
    _, provider_ledger = _bound_catalog_identity(
        catalog=selected_inputs.provider_ledger,
        manifest_snapshot=selected_inputs.provider_ledger_manifest,
        catalog_artifact_id=_PROVIDER_LEDGER_ARTIFACT_ID,
        catalog_schema_name=_PROVIDER_LEDGER_SCHEMA_NAME,
        catalog_schema_version_field="catalog_schema_version",
        manifest_identity_artifact_id="identity/provider-ledger-generation.json",
    )
    _, baseline = _bound_catalog_identity(
        catalog=selected_inputs.baseline,
        manifest_snapshot=selected_inputs.baseline_manifest,
        catalog_artifact_id=_BASELINE_ARTIFACT_ID,
        catalog_schema_name=_BASELINE_SCHEMA_NAME,
        catalog_schema_version_field="baseline_schema_version",
        manifest_identity_artifact_id="identity/portfolio-baseline-generation.json",
    )
    payload = {
        "schema_version": 1,
        "artifact_id": "selected-input-identities",
        "as_of_date": selected_inputs.generation_as_of_date.isoformat(),
        "provider_ledger": provider_ledger,
        "baseline": baseline,
        "flow_classification_policy": _snapshot_identity(
            selected_inputs.flow_classification_policy
        ),
        "retrieval_receipts": [
            {"receipt_id": receipt_id, **_snapshot_identity(receipt)}
            for receipt_id, receipt in sorted(
                selected_inputs.retrieval_receipts.items()
            )
        ],
        "code_sources": {
            key: {
                "artifact_id": _CODE_SOURCE_ARTIFACT_IDS[key],
                **_snapshot_identity(snapshot),
            }
            for key, snapshot in (
                ("epoch_producer", selected_inputs.epoch_producer_source),
                ("generation_builder", selected_inputs.generation_builder_source),
                ("independent_verifier", selected_inputs.independent_verifier_source),
            )
        },
    }
    if selected_inputs.receipt_authority is not None:
        assert selected_inputs.admission_policy is not None
        assert selected_inputs.manifest_schema is not None
        assert selected_inputs.membership_census is not None
        payload["receipt_authority"] = _snapshot_identity(
            selected_inputs.receipt_authority
        )
        payload["admission_policy"] = _snapshot_identity(
            selected_inputs.admission_policy
        )
        payload["manifest_schema"] = _snapshot_identity(selected_inputs.manifest_schema)
        payload["membership_census"] = _snapshot_identity(
            selected_inputs.membership_census
        )
    if selected_inputs.current_epoch_crosswalk is not None:
        payload["current_epoch_crosswalk"] = _snapshot_identity(
            selected_inputs.current_epoch_crosswalk
        )
    if selected_inputs.sleeve_facts:
        payload["sleeve_facts"] = [
            _snapshot_identity(snapshot)
            for snapshot in sorted(
                selected_inputs.sleeve_facts, key=lambda item: item.sha256
            )
        ]
    return payload


def build_selected_input_identities(
    selected_inputs: AccountManifestSelectedInputs,
) -> bytes:
    """Return the closed canonical selected-input identity payload."""
    return canonical_json_bytes(_selected_input_identities(selected_inputs))


def validate_selected_input_identities(
    payload: Mapping[str, object],
    selected_inputs: AccountManifestSelectedInputs,
) -> None:
    """Reject any mutation of the frozen selected-input identity payload."""
    if not isinstance(payload, Mapping):
        raise AccountAssignmentPerimeterError("payload must be an object")
    if dict(payload) != _selected_input_identities(selected_inputs):
        raise AccountAssignmentPerimeterError(
            "payload differs from expected selected inputs"
        )


def _semantic_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _nonempty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AccountAssignmentPerimeterError(
            f"{field_name} must be a non-empty string"
        )
    return value


def _iso_date(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AccountAssignmentPerimeterError(
            f"{field_name} must be a canonical ISO date"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise AccountAssignmentPerimeterError(
            f"{field_name} must be a canonical ISO date"
        ) from exc
    if parsed.isoformat() != value:
        raise AccountAssignmentPerimeterError(
            f"{field_name} must be a canonical ISO date"
        )
    return value


def _baseline_payload(
    baseline: InputSnapshot,
    *,
    assignments_override: Sequence[Mapping[str, object]] | None = None,
) -> tuple[dict[str, object], dict[str, str], list[dict[str, object]]]:
    if not isinstance(baseline, InputSnapshot):
        raise AccountAssignmentPerimeterError("baseline must be an InputSnapshot")
    try:
        payload = baseline.json_object("portfolio baseline")
    except ValueError as exc:
        raise AccountAssignmentPerimeterError(
            "portfolio baseline is not strict JSON"
        ) from exc
    schema_version = payload.get("baseline_schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 0
    ):
        raise AccountAssignmentPerimeterError(
            "baseline_schema_version must be a non-negative integer"
        )
    _iso_date(payload.get("as_of_date"), "baseline.as_of_date")
    accounts = payload.get("accounts")
    assignments = (
        payload.get("assignments")
        if assignments_override is None
        else list(assignments_override)
    )
    if not isinstance(accounts, list) or not isinstance(assignments, list):
        raise AccountAssignmentPerimeterError(
            "baseline accounts and assignments must be lists"
        )
    if not accounts or not assignments:
        raise AccountAssignmentPerimeterError(
            "baseline accounts and assignments must not be empty"
        )
    providers: dict[str, str] = {}
    for index, account in enumerate(accounts):
        if not isinstance(account, Mapping):
            raise AccountAssignmentPerimeterError(
                f"baseline account {index} must be an object"
            )
        account_id = _nonempty_text(
            account.get("account_id"), f"account[{index}].account_id"
        )
        provider = _nonempty_text(account.get("provider"), f"account[{index}].provider")
        if account_id in providers:
            raise AccountAssignmentPerimeterError(
                f"duplicate baseline account_id '{account_id}'"
            )
        providers[account_id] = provider
    normalized_assignments: list[dict[str, object]] = []
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            raise AccountAssignmentPerimeterError(
                f"baseline assignment {index} must be an object"
            )
        normalized_assignments.append(dict(assignment))
    try:
        perimeter = partition_assignments(normalized_assignments)
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(str(exc)) from exc
    assignment_account_ids = set(perimeter.full_chains)
    if not assignment_account_ids <= set(providers):
        raise AccountAssignmentPerimeterError("baseline assignment account is unknown")
    return (
        payload,
        {
            account_id: provider
            for account_id, provider in providers.items()
            if account_id in assignment_account_ids
        },
        normalized_assignments,
    )


def _sleeve_manifest_facts(
    selected_inputs: AccountManifestSelectedInputs,
) -> SleeveManifestFacts | None:
    """The manifest facts derived from the selected sleeve-facts artifacts."""

    if not selected_inputs.sleeve_facts:
        return None
    _, providers, _ = _baseline_payload(selected_inputs.baseline)
    try:
        return derive_sleeve_manifest_facts(
            selected_inputs.sleeve_facts, account_providers=providers
        )
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(str(exc)) from exc


def _effective_perimeter(
    selected_inputs: AccountManifestSelectedInputs,
) -> tuple[
    dict[str, object],
    dict[str, str],
    list[dict[str, object]],
    SleeveManifestFacts | None,
]:
    """The baseline perimeter after the derived sleeves joined it.

    A residual parent stops being a strategy account and every derived sleeve
    (with the perimeter pseudo-account) appears as its own account.
    """

    baseline_payload, providers, assignments = _baseline_payload(
        selected_inputs.baseline
    )
    sleeve = _sleeve_manifest_facts(selected_inputs)
    if sleeve is None:
        return baseline_payload, providers, assignments, None
    try:
        assignments = apply_sleeve_assignments(assignments, sleeve)
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(str(exc)) from exc
    payload = dict(baseline_payload)
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise AccountAssignmentPerimeterError("baseline accounts must be a list")
    payload["accounts"] = [*accounts, *(dict(item) for item in sleeve.accounts)]
    providers = {
        **providers,
        **{str(item["account_id"]): str(item["provider"]) for item in sleeve.accounts},
    }
    try:
        perimeter = partition_assignments(assignments)
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(str(exc)) from exc
    return (
        payload,
        {
            account_id: provider
            for account_id, provider in providers.items()
            if account_id in set(perimeter.full_chains)
        },
        assignments,
        sleeve,
    )


def _assignment_preimage(assignment: Mapping[str, object]) -> dict[str, object]:
    return {
        "account_id": assignment["account_id"],
        "strategy_id": assignment["strategy_id"],
        "strategy_mapping_disposition": assignment["strategy_mapping_disposition"],
        "effective_start": assignment["effective_start"],
        "effective_end": assignment["effective_end"],
    }


def _row_sort_key(
    row: Mapping[str, object],
) -> tuple[str, bool, str, bool, str, bool, str, str]:
    effective_start = row["effective_start"]
    effective_end = row["effective_end"]
    strategy_id = row["strategy_id"]
    assert isinstance(effective_start, str) or effective_start is None
    assert isinstance(effective_end, str) or effective_end is None
    assert isinstance(strategy_id, str) or strategy_id is None
    return (
        str(row["account_id"]),
        effective_start is not None,
        effective_start or "",
        effective_end is None,
        effective_end or "",
        strategy_id is None,
        strategy_id or "",
        str(row["strategy_mapping_disposition"]),
    )


def _in_interval(fact_date: str, assignment: Mapping[str, object]) -> bool:
    effective_start = assignment["effective_start"]
    effective_end = assignment["effective_end"]
    assert isinstance(effective_start, str) or effective_start is None
    assert isinstance(effective_end, str) or effective_end is None
    return (effective_start is None or fact_date >= effective_start) and (
        effective_end is None or fact_date <= effective_end
    )


def _index_rows(
    assignments: list[dict[str, object]], providers: Mapping[str, str]
) -> tuple[
    list[dict[str, object]],
    dict[tuple[str, str | None, str | None, str | None], dict[str, object]],
]:
    rows: list[dict[str, object]] = []
    by_interval: dict[
        tuple[str, str | None, str | None, str | None], dict[str, object]
    ] = {}
    identities: set[str] = set()
    for assignment in assignments:
        preimage = _assignment_preimage(assignment)
        account_id = preimage["account_id"]
        effective_start = preimage["effective_start"]
        effective_end = preimage["effective_end"]
        strategy_id = preimage["strategy_id"]
        disposition = preimage["strategy_mapping_disposition"]
        assert isinstance(account_id, str)
        assert isinstance(effective_start, str) or effective_start is None
        assert isinstance(effective_end, str) or effective_end is None
        assert isinstance(strategy_id, str) or strategy_id is None
        assert isinstance(disposition, str)
        digest = _semantic_digest(preimage)
        if digest in identities:
            raise AccountAssignmentPerimeterError("duplicate assignment identity")
        identities.add(digest)
        row = {
            "assignment_identity": {"sha256": digest},
            "account_id": account_id,
            "provider": providers[account_id],
            "strategy_mapping_disposition": disposition,
            "strategy_id": strategy_id,
            "effective_start": effective_start,
            "effective_end": effective_end,
            "calculation_eligible": disposition == "STRATEGY_MAPPED",
            "available_nav": 0,
            "unavailable_nav": 0,
            "raw_flows": 0,
            "classified_flows": 0,
        }
        interval_key = (account_id, effective_start, effective_end, strategy_id)
        by_interval[interval_key] = row
        rows.append(row)
    rows.sort(key=_row_sort_key)
    return rows, by_interval


def _assignment_row_for_fact(
    *,
    account_id: str,
    fact_date: str,
    assignments: Sequence[Mapping[str, object]],
    rows_by_interval: Mapping[
        tuple[str, str | None, str | None, str | None], dict[str, object]
    ],
    known_accounts: Mapping[str, str],
) -> dict[str, object] | None:
    if account_id not in known_accounts:
        return None
    matches = [
        assignment
        for assignment in assignments
        if _in_interval(fact_date, assignment)
        and assignment["account_id"] == account_id
    ]
    if len(matches) == 2:
        historical = [
            assignment
            for assignment in matches
            if assignment["strategy_mapping_disposition"] == "HISTORICAL_POOL_ONLY"
            and assignment["effective_start"] is not None
            and assignment["effective_end"] == fact_date
        ]
        mapped = [
            assignment
            for assignment in matches
            if assignment["strategy_mapping_disposition"] == "STRATEGY_MAPPED"
            and assignment["effective_start"] == fact_date
        ]
        if len(historical) == 1 and len(mapped) == 1:
            matches = mapped
    if not matches:
        return None
    if len(matches) != 1:
        raise AccountAssignmentPerimeterError(
            "fact has multiple admissible assignment owners"
        )
    match = matches[0]
    key = (
        account_id,
        match["effective_start"],
        match["effective_end"],
        match["strategy_id"],
    )
    assert isinstance(key[1], str) or key[1] is None
    assert isinstance(key[2], str) or key[2] is None
    assert isinstance(key[3], str) or key[3] is None
    return rows_by_interval[key]


_GENERAL_DISPOSITION = "GENERAL_OUT_OF_STRATEGY_PERIMETER"


def _general_reason(
    account_id: str,
    fact_date: str,
    assignments: Sequence[Mapping[str, object]],
    known_accounts: Mapping[str, str],
) -> str | None:
    """Why a fact outside every assignment interval is GENERAL, or None if undisposed.

    Derived only from the account's STRATEGY_MAPPED assignment rows: nothing
    that predates the account's first defined strategy, or belongs to an account
    no strategy was ever assigned to, can be something a defined strategy
    depends on.  A fact after the last closed strategy interval of a
    strategy-assigned account may still be that strategy's post-end cash, so it
    stays undisposed (blocked) until a rule declares it out of the product.
    """

    mapped = [
        assignment
        for assignment in assignments
        if assignment["account_id"] == account_id
        and assignment["strategy_mapping_disposition"] == "STRATEGY_MAPPED"
    ]
    if not mapped:
        # An account outside the selected baseline is named as such, so the
        # disposition never reads as a statement about a selected account.
        return (
            "NO_STRATEGY_ASSIGNED_TO_ACCOUNT"
            if account_id in known_accounts
            else "ACCOUNT_NOT_IN_SELECTED_BASELINE"
        )
    if fact_date < min(str(assignment["effective_start"]) for assignment in mapped):
        return "PREDATES_FIRST_STRATEGY_ASSIGNMENT"
    return None


_POST_END_DISPOSITIONS = {
    "INCLUDED_PRODUCT_EPOCH": "POST_END_TAIL_ATTRIBUTED_TO_STRATEGY",
    "EXCLUDED_DEPRECATED_HISTORY_ONLY": "POST_END_HISTORY_EXCLUDED_BY_PRODUCT_DISPOSITION",
    "EXCLUDED_IMMATERIAL": "POST_END_HISTORY_EXCLUDED_BY_PRODUCT_DISPOSITION",
}


def _post_end_target(
    account_id: str,
    fact_date: str,
    assignments: Sequence[Mapping[str, object]],
    crosswalk_dispositions: Mapping[tuple[str, str, str, str], str],
) -> tuple[str, str] | None:
    """The strategy a fact after an account's last closed interval belongs to.

    The fact is the post-end tail of that strategy's closed epoch, or excluded
    history when the owner crosswalk declares the epoch outside the product.
    None when every mapped interval is not closed, the fact is inside the
    chain, or the crosswalk does not name the epoch (the fact stays blocked).
    """

    mapped = [
        assignment
        for assignment in assignments
        if assignment["account_id"] == account_id
        and assignment["strategy_mapping_disposition"] == "STRATEGY_MAPPED"
    ]
    if not mapped or any(assignment["effective_end"] is None for assignment in mapped):
        return None
    last = max(
        mapped,
        key=lambda assignment: (
            str(assignment["effective_end"]),
            str(assignment["effective_start"]),
        ),
    )
    if fact_date <= str(last["effective_end"]):
        return None
    disposition = crosswalk_dispositions.get(
        (
            account_id,
            str(last["strategy_id"]),
            str(last["effective_start"]),
            str(last["effective_end"]),
        )
    )
    if disposition is None:
        return None
    return str(last["strategy_id"]), disposition


def _post_end_summaries(
    facts: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Per strategy and disposition: how many post-end facts and over which dates.

    The perimeter is count-only and numeric-free, so no amount appears here; the
    counts and the date span say exactly which facts to read for their value.
    """

    groups: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    for fact in facts:
        groups.setdefault(
            (
                str(fact["strategy_id"]),
                str(fact["disposition"]),
                str(fact["product_epoch_disposition"]),
            ),
            [],
        ).append(fact)
    summaries: list[dict[str, object]] = []
    for (strategy_id, disposition, product), members in sorted(groups.items()):
        dates = sorted(str(fact["fact_date"]) for fact in members)
        summaries.append(
            {
                "strategy_id": strategy_id,
                "disposition": disposition,
                "product_epoch_disposition": product,
                "nav_fact_count": sum(
                    1 for fact in members if fact["source_kind"] == "NAV"
                ),
                "raw_flow_fact_count": sum(
                    1 for fact in members if fact["source_kind"] == "RAW_FLOW"
                ),
                "first_fact_date": dates[0],
                "last_fact_date": dates[-1],
            }
        )
    return summaries


def _count_facts(
    *,
    nav_facts: Sequence[Mapping[str, object]],
    raw_flow_facts: Sequence[Mapping[str, object]],
    classified_flow_facts: Sequence[Mapping[str, object]],
    assignments: Sequence[Mapping[str, object]],
    rows_by_interval: Mapping[
        tuple[str, str | None, str | None, str | None], dict[str, object]
    ],
    known_accounts: Mapping[str, str],
    provider_ledger_as_of_date: date,
    crosswalk_dispositions: Mapping[tuple[str, str, str, str], str] | None = None,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    unassigned: list[dict[str, object]] = []
    general: list[dict[str, object]] = []
    post_end: list[dict[str, object]] = []
    dispositions = crosswalk_dispositions or {}

    def dispose(
        entry: dict[str, object],
        account_id: str,
        fact_date: str,
    ) -> None:
        reason = _general_reason(account_id, fact_date, assignments, known_accounts)
        if reason is not None:
            general.append(
                {
                    **entry,
                    "status": "GENERAL",
                    "disposition": _GENERAL_DISPOSITION,
                    "reason": reason,
                }
            )
            return
        target = _post_end_target(account_id, fact_date, assignments, dispositions)
        if target is not None:
            strategy_id, product = target
            post_end.append(
                {
                    **entry,
                    "status": "POST_END",
                    "disposition": _POST_END_DISPOSITIONS[product],
                    "strategy_id": strategy_id,
                    "product_epoch_disposition": product,
                }
            )
            return
        unassigned.append(
            {
                **entry,
                "status": "BLOCKED",
                "blocker_code": (
                    "NO_ASSIGNMENT_MATCH"
                    if account_id in known_accounts
                    else "ACCOUNT_NOT_IN_SELECTED_BASELINE"
                ),
            }
        )

    nav_record_ids: set[str] = set()
    nav_account_dates: set[tuple[str, str]] = set()
    for index, fact in enumerate(nav_facts):
        if not isinstance(fact, Mapping):
            raise AccountAssignmentPerimeterError(f"NAV fact {index} must be an object")
        record_id = _nonempty_text(fact.get("record_id"), f"NAV fact {index}.record_id")
        if record_id in nav_record_ids:
            raise AccountAssignmentPerimeterError("duplicate NAV record_id")
        nav_record_ids.add(record_id)
        account_id = _nonempty_text(
            fact.get("account_id"), f"NAV fact {index}.account_id"
        )
        as_of_date = _iso_date(fact.get("as_of_date"), f"NAV fact {index}.as_of_date")
        _reject_provider_fact_after_ledger_date(as_of_date, provider_ledger_as_of_date)
        account_date = (account_id, as_of_date)
        if account_date in nav_account_dates:
            raise AccountAssignmentPerimeterError("duplicate NAV account_id/as_of_date")
        nav_account_dates.add(account_date)
        nav = fact.get("nav")
        if not isinstance(nav, Mapping):
            raise AccountAssignmentPerimeterError(
                f"NAV fact {index}.nav must be an object"
            )
        status = nav.get("status")
        if status not in {"AVAILABLE", "UNAVAILABLE"}:
            raise AccountAssignmentPerimeterError(
                "NAV status must be AVAILABLE or UNAVAILABLE"
            )
        row = _assignment_row_for_fact(
            account_id=account_id,
            fact_date=as_of_date,
            assignments=assignments,
            rows_by_interval=rows_by_interval,
            known_accounts=known_accounts,
        )
        if row is None:
            dispose(
                {
                    "source_kind": "NAV",
                    "source_identity": {"record_id": record_id},
                    "account_id": account_id,
                    "fact_date": as_of_date,
                    "assignment_match_count": 0,
                },
                account_id,
                as_of_date,
            )
        else:
            row["available_nav" if status == "AVAILABLE" else "unavailable_nav"] += 1

    raw_record_ids: set[str] = set()
    raw_by_id: dict[str, tuple[str, str, dict[str, object] | None]] = {}
    for index, fact in enumerate(raw_flow_facts):
        if not isinstance(fact, Mapping):
            raise AccountAssignmentPerimeterError(
                f"raw flow fact {index} must be an object"
            )
        record_id = _nonempty_text(
            fact.get("record_id"), f"raw flow fact {index}.record_id"
        )
        if record_id in raw_record_ids:
            raise AccountAssignmentPerimeterError("duplicate raw flow record_id")
        raw_record_ids.add(record_id)
        account_id = _nonempty_text(
            fact.get("account_id"), f"raw flow fact {index}.account_id"
        )
        event_date = _iso_date(
            fact.get("event_date"), f"raw flow fact {index}.event_date"
        )
        _reject_provider_fact_after_ledger_date(event_date, provider_ledger_as_of_date)
        row = _assignment_row_for_fact(
            account_id=account_id,
            fact_date=event_date,
            assignments=assignments,
            rows_by_interval=rows_by_interval,
            known_accounts=known_accounts,
        )
        raw_by_id[record_id] = (account_id, event_date, row)
        if row is not None:
            row["raw_flows"] += 1

    classified_event_ids: set[str] = set()
    classified_source_record_ids: set[str] = set()
    for index, fact in enumerate(classified_flow_facts):
        if not isinstance(fact, Mapping):
            raise AccountAssignmentPerimeterError(
                f"classified flow fact {index} must be an object"
            )
        event_id = _nonempty_text(
            fact.get("event_id"), f"classified flow fact {index}.event_id"
        )
        if event_id in classified_event_ids:
            raise AccountAssignmentPerimeterError("duplicate classified flow event_id")
        classified_event_ids.add(event_id)
        source_record_id = _nonempty_text(
            fact.get("source_record_id"),
            f"classified flow fact {index}.source_record_id",
        )
        if source_record_id in classified_source_record_ids:
            raise AccountAssignmentPerimeterError(
                "duplicate classified flow source_record_id"
            )
        classified_source_record_ids.add(source_record_id)
        if source_record_id not in raw_by_id:
            raise AccountAssignmentPerimeterError(
                "classified flow source_record_id has no raw flow"
            )
        account_id = _nonempty_text(
            fact.get("account_id"), f"classified flow fact {index}.account_id"
        )
        event_date = _iso_date(
            fact.get("event_date"), f"classified flow fact {index}.event_date"
        )
        _reject_provider_fact_after_ledger_date(event_date, provider_ledger_as_of_date)
        row = _assignment_row_for_fact(
            account_id=account_id,
            fact_date=event_date,
            assignments=assignments,
            rows_by_interval=rows_by_interval,
            known_accounts=known_accounts,
        )
        raw_account_id, raw_event_date, raw_row = raw_by_id[source_record_id]
        if account_id != raw_account_id or event_date != raw_event_date:
            raise AccountAssignmentPerimeterError(
                "raw/classified flow lineage disagrees"
            )
        if (row is None) != (raw_row is None) or (
            row is not None and row is not raw_row
        ):
            raise AccountAssignmentPerimeterError(
                "raw/classified flow assignment disagrees"
            )
        if row is None:
            dispose(
                {
                    "source_kind": "RAW_FLOW",
                    "source_identity": {"record_id": source_record_id},
                    "classified_projection_identity": {
                        "event_id": event_id,
                        "source_record_id": source_record_id,
                    },
                    "account_id": account_id,
                    "fact_date": event_date,
                    "assignment_match_count": 0,
                },
                account_id,
                event_date,
            )
        else:
            row["classified_flows"] += 1
    if classified_source_record_ids != raw_record_ids:
        raise AccountAssignmentPerimeterError(
            "raw/classified flow lineage is not one-to-one"
        )
    for facts in (unassigned, general, post_end):
        facts.sort(
            key=lambda item: (
                0 if item["source_kind"] == "NAV" else 1,
                str(item["account_id"]),
                str(item["fact_date"]),
                str(item["source_identity"]["record_id"]),
            )
        )
    return (
        unassigned,
        general,
        post_end,
        _post_end_summaries(post_end),
    )


def _expected_payload(
    *,
    selected_inputs: AccountManifestSelectedInputs,
    nav_facts: Sequence[Mapping[str, object]],
    raw_flow_facts: Sequence[Mapping[str, object]],
    classified_flow_facts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if type(selected_inputs) is not AccountManifestSelectedInputs:
        raise AccountAssignmentPerimeterError(
            "selected_inputs must be an AccountManifestSelectedInputs"
        )
    baseline_payload, providers, assignments, _ = _effective_perimeter(selected_inputs)
    ledger_as_of_date = _selected_top_level_date(selected_inputs.provider_ledger)
    try:
        selected_ledger = selected_inputs.provider_ledger.json_object(
            "selected provider ledger"
        )
    except ValueError:
        raise AccountAssignmentPerimeterError(
            "selected provider ledger is invalid"
        ) from None
    _validate_provider_generation_dates(selected_ledger, ledger_as_of_date)
    rows, rows_by_interval = _index_rows(assignments, providers)
    selected_accounts = baseline_payload.get("accounts")
    if not isinstance(selected_accounts, list):
        raise AccountAssignmentPerimeterError("baseline accounts must be a list")
    selected_providers = {
        _nonempty_text(
            account.get("account_id"), "baseline account_id"
        ): _nonempty_text(account.get("provider"), "baseline account provider")
        for account in selected_accounts
        if isinstance(account, Mapping)
    }
    if len(selected_providers) != len(selected_accounts):
        raise AccountAssignmentPerimeterError("baseline account shape is invalid")
    crosswalk_dispositions: dict[tuple[str, str, str, str], str] = {}
    if selected_inputs.current_epoch_crosswalk is not None:
        _validate_current_epoch_crosswalk(
            selected_inputs.baseline,
            selected_inputs.current_epoch_crosswalk,
            baseline_generation_id=selected_inputs.baseline_generation_id,
        )
        for crosswalk_row in selected_inputs.current_epoch_crosswalk.json_object(
            "current epoch crosswalk"
        )["rows"]:
            crosswalk_dispositions[
                (
                    str(crosswalk_row["account_id"]),
                    str(crosswalk_row["strategy_id"]),
                    str(crosswalk_row["effective_start"]),
                    str(crosswalk_row["effective_end"]),
                )
            ] = str(crosswalk_row["product_epoch_disposition"])
    unassigned_facts, general_facts, post_end_facts, post_end_summaries = _count_facts(
        nav_facts=nav_facts,
        raw_flow_facts=raw_flow_facts,
        classified_flow_facts=classified_flow_facts,
        assignments=assignments,
        rows_by_interval=rows_by_interval,
        known_accounts=selected_providers,
        provider_ledger_as_of_date=ledger_as_of_date,
        crosswalk_dispositions=crosswalk_dispositions,
    )
    full_perimeter_preimage = [
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
    try:
        accepted_identity = partition_assignments(assignments).full_perimeter_identity
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(str(exc)) from exc
    full_perimeter_identity = {
        "sha256": _semantic_digest(full_perimeter_preimage),
        "count": len(full_perimeter_preimage),
    }
    if accepted_identity != full_perimeter_identity:
        raise AccountAssignmentPerimeterError(
            "accepted assignment partition identity differs from perimeter identity"
        )
    partition_counts = Counter(str(row["strategy_mapping_disposition"]) for row in rows)
    return {
        "schema_version": 1,
        "artifact_id": _ARTIFACT_ID,
        "as_of_date": selected_inputs.generation_as_of_date.isoformat(),
        "selected_baseline_identity": {
            "generation_id": selected_inputs.baseline_generation_id,
            "artifact": {
                "artifact_id": _BASELINE_ARTIFACT_ID,
                "as_of_date": baseline_payload["as_of_date"],
                "schema": {
                    "name": _BASELINE_SCHEMA_NAME,
                    "version": str(baseline_payload["baseline_schema_version"]),
                },
                "sha256": selected_inputs.baseline.sha256,
                "size_bytes": selected_inputs.baseline.size_bytes,
            },
        },
        "full_perimeter_identity": full_perimeter_identity,
        "selected_baseline_account_count": len(selected_providers),
        "account_count": len(providers),
        "assignment_count": len(rows),
        "partition_counts": {
            disposition: partition_counts[disposition] for disposition in _DISPOSITIONS
        },
        "fact_bijection_status": (
            "BLOCKED_UNASSIGNED_FACTS" if unassigned_facts else "ONE_TO_ONE_VERIFIED"
        ),
        "unassigned_fact_count": len(unassigned_facts),
        "unassigned_projection_count": sum(
            2 if fact["source_kind"] == "RAW_FLOW" else 1 for fact in unassigned_facts
        ),
        "unassigned_facts": unassigned_facts,
        # Present only when a fact was disposed GENERAL, so every perimeter
        # without one keeps its bytes.
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


def build_account_assignment_perimeter(
    *,
    selected_inputs: AccountManifestSelectedInputs,
    nav_facts: Sequence[Mapping[str, object]],
    raw_flow_facts: Sequence[Mapping[str, object]],
    classified_flow_facts: Sequence[Mapping[str, object]],
) -> bytes:
    """Return the canonical account-assignment perimeter JSON bytes."""
    return canonical_json_bytes(
        _expected_payload(
            selected_inputs=selected_inputs,
            nav_facts=nav_facts,
            raw_flow_facts=raw_flow_facts,
            classified_flow_facts=classified_flow_facts,
        )
    )


def validate_account_assignment_perimeter(
    payload: Mapping[str, object],
    *,
    selected_inputs: AccountManifestSelectedInputs,
    nav_facts: Sequence[Mapping[str, object]],
    raw_flow_facts: Sequence[Mapping[str, object]],
    classified_flow_facts: Sequence[Mapping[str, object]],
) -> None:
    """Reject any semantic mutation of the expected producer payload."""
    if not isinstance(payload, Mapping):
        raise AccountAssignmentPerimeterError("payload must be an object")
    expected = _expected_payload(
        selected_inputs=selected_inputs,
        nav_facts=nav_facts,
        raw_flow_facts=raw_flow_facts,
        classified_flow_facts=classified_flow_facts,
    )
    if dict(payload) != expected:
        raise AccountAssignmentPerimeterError("payload differs from expected perimeter")


def _c3_artifact(
    snapshot: InputSnapshot,
    artifact_id: str,
    schema_name: str,
    as_of_date: str,
    schema_version: str = "1",
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "as_of_date": as_of_date,
        "schema": {"name": schema_name, "version": schema_version},
        **_snapshot_identity(snapshot),
    }


def _c3_money(
    value: object,
    field_name: str,
    *,
    allow_unavailable_reason: bool = False,
    allow_asset_currency: bool = False,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AccountAssignmentPerimeterError(f"{field_name} money contract is invalid")
    keys = set(value)
    base_keys = {"currency", "status", "value"}
    if keys != base_keys and not (
        allow_unavailable_reason
        and keys == base_keys | {"reason"}
        and value.get("status") == "UNAVAILABLE"
    ):
        raise AccountAssignmentPerimeterError(f"{field_name} money contract is invalid")
    currency, status, amount = value["currency"], value["status"], value["value"]
    currency_pattern = r"[A-Z]{3,12}" if allow_asset_currency else _CURRENCY
    if (
        not isinstance(currency, str)
        or re.fullmatch(currency_pattern, currency) is None
    ):
        raise AccountAssignmentPerimeterError(f"{field_name} currency is invalid")
    if status == "UNAVAILABLE":
        if amount is not None:
            raise AccountAssignmentPerimeterError(
                f"{field_name} unavailable value must be null"
            )
        normalized = {"currency": currency, "status": status, "value": None}
        if "reason" in value:
            reason = value["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise AccountAssignmentPerimeterError(
                    f"{field_name} unavailable reason is invalid"
                )
            normalized["reason"] = reason.strip()
        return normalized
    if status != "AVAILABLE" or not isinstance(amount, str):
        raise AccountAssignmentPerimeterError(f"{field_name} money status is invalid")
    try:
        decimal = Decimal(amount)
    except (InvalidOperation, ValueError):
        raise AccountAssignmentPerimeterError(
            f"{field_name} value is invalid"
        ) from None
    if not decimal.is_finite():
        raise AccountAssignmentPerimeterError(f"{field_name} value is invalid")
    if _CANONICAL_DECIMAL.fullmatch(amount) is None or (
        amount.startswith("-") and decimal.is_zero()
    ):
        raise AccountAssignmentPerimeterError(f"{field_name} value is not canonical")
    return {"currency": currency, "status": status, "value": amount}


def _c3_account_id(value: object, field_name: str) -> str:
    account_id = _nonempty_text(value, field_name)
    if _INTERNAL_ACCOUNT_ID.fullmatch(account_id) is None:
        raise AccountAssignmentPerimeterError(f"{field_name} is invalid")
    return account_id


def _c3_flow_scope_metadata(
    row: Mapping[str, object], record_id: str
) -> dict[str, object]:
    """Retain the provider mapping fields that bound calculation eligibility."""

    metadata: dict[str, object] = {}
    if "provider_account_scope" in row:
        metadata["provider_account_scope"] = _nonempty_text(
            row.get("provider_account_scope"),
            f"flow {record_id} provider_account_scope",
        )
    if "mapping_status" in row:
        mapping_status = row.get("mapping_status")
        if mapping_status not in {"MAPPED", "UNMAPPED"}:
            raise AccountAssignmentPerimeterError(
                f"flow {record_id} mapping_status is invalid"
            )
        metadata["mapping_status"] = mapping_status
    if "strategy_id" in row:
        strategy_id = row.get("strategy_id")
        metadata["strategy_id"] = (
            None
            if strategy_id is None
            else _c3_account_id(strategy_id, f"flow {record_id} strategy_id")
        )
    return metadata


def _require_sleeve_reconciliation(
    navs: Sequence[Mapping[str, object]], sleeve: SleeveManifestFacts
) -> None:
    """The sleeves and the perimeter residual add up to the parent account."""

    parent_navs = {
        (str(item["account_id"]), str(item["as_of_date"])): item["nav"] for item in navs
    }
    for row in sleeve.reconciliations:
        parent, day = str(row["parent_account_id"]), str(row["day"])
        nav = parent_navs.get((parent, day))
        if not isinstance(nav, Mapping) or nav.get("status") != "AVAILABLE":
            raise AccountAssignmentPerimeterError(
                f"parent account {parent} has no AVAILABLE NAV on {day} to "
                "reconcile its sleeves to"
            )
        try:
            derived = Decimal(str(row["sleeves_nav"])) + Decimal(
                str(row["perimeter_nav"])
            )
            difference = abs(Decimal(str(nav["value"])) - derived)
            tolerance = Decimal(str(row["tolerance"]))
        except (InvalidOperation, ValueError):
            raise AccountAssignmentPerimeterError(
                "sleeve reconciliation figures are invalid"
            ) from None
        if difference > tolerance:
            raise AccountAssignmentPerimeterError(
                f"sleeves and perimeter residual do not reconcile to parent "
                f"{parent} on {day}: parent NAV {nav['value']}, sleeves "
                f"{row['sleeves_nav']} + residual {row['perimeter_nav']} "
                f"(difference {difference}, tolerance {tolerance})"
            )


def _require_sleeve_references(
    flows: Sequence[Mapping[str, object]], sleeve: SleeveManifestFacts
) -> None:
    """A parent-ledger leg is pairing evidence: it must already be one ledger flow."""

    for reference in sleeve.references:
        sign = 1 if reference["class"] == "INTERNAL_IN" else -1
        wanted = Decimal(str(reference["amount"])) * sign
        matches = [
            flow
            for flow in flows
            if flow["account_id"] == reference["account_id"]
            and flow["event_date"] == reference["event_date"]
            and isinstance(flow["amount"], Mapping)
            and flow["amount"].get("status") == "AVAILABLE"
            and Decimal(str(flow["amount"].get("value"))) == wanted
        ]
        if len(matches) != 1:
            raise AccountAssignmentPerimeterError(
                f"parent-ledger reference {reference['pair_id']} "
                f"({reference['class']} {reference['amount']} on "
                f"{reference['event_date']} for {reference['account_id']}) matches "
                f"{len(matches)} ledger flows, not exactly one"
            )
        if not str(matches[0]["flow_type"]).startswith("TRANSFER:INTERNAL:"):
            raise AccountAssignmentPerimeterError(
                f"parent-ledger reference {reference['pair_id']} is ledger flow "
                f"{matches[0]['record_id']} of type {matches[0]['flow_type']}, "
                "which does not pair under the TRANSFER:INTERNAL: rule"
            )


def _c3_ledger(
    selected_inputs: AccountManifestSelectedInputs,
    *,
    enforce_global_accounting_currency: bool = True,
    allow_asset_flow_currency: bool = False,
    sleeve: SleeveManifestFacts | None = None,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    str | None,
]:
    try:
        ledger = selected_inputs.provider_ledger.json_object("selected provider ledger")
    except ValueError:
        raise AccountAssignmentPerimeterError(
            "selected provider ledger is invalid"
        ) from None
    ledger_as_of_date = _selected_top_level_date(selected_inputs.provider_ledger)
    if not isinstance(ledger.get("catalog_schema_version"), int):
        raise AccountAssignmentPerimeterError(
            "provider ledger catalog schema version is invalid"
        )
    navs, flows = ledger.get("nav_observations"), ledger.get("flows")
    anchors = ledger.get("capital_anchors", [])
    if (
        not isinstance(navs, list)
        or not isinstance(flows, list)
        or not isinstance(anchors, list)
    ):
        raise AccountAssignmentPerimeterError("provider ledger facts must be lists")
    _validate_provider_generation_dates(ledger, ledger_as_of_date)
    normalized_navs: list[dict[str, object]] = []
    available_currencies: set[str] = set()
    nav_ids: set[str] = set()
    for index, row in enumerate(navs):
        if not isinstance(row, Mapping) or not {
            "record_id",
            "account_id",
            "as_of_date",
            "nav",
        } <= set(row):
            raise AccountAssignmentPerimeterError(
                "provider ledger NAV shape is invalid"
            )
        record_id = _nonempty_text(row.get("record_id"), "NAV record_id")
        if record_id in nav_ids:
            raise AccountAssignmentPerimeterError(
                "duplicate provider ledger NAV record_id"
            )
        nav_ids.add(record_id)
        account_id = _c3_account_id(row.get("account_id"), "NAV account_id")
        as_of_date = _iso_date(row.get("as_of_date"), "NAV as_of_date")
        _reject_provider_fact_after_ledger_date(as_of_date, ledger_as_of_date)
        nav = _c3_money(row.get("nav"), f"NAV {index}", allow_unavailable_reason=True)
        if nav["status"] == "AVAILABLE":
            available_currencies.add(str(nav["currency"]))
        normalized_nav: dict[str, object] = {
            "record_id": record_id,
            "account_id": account_id,
            "as_of_date": as_of_date,
            "nav": nav,
        }
        if "value_basis" in row:
            normalized_nav["value_basis"] = _nonempty_text(
                row.get("value_basis"), f"NAV {record_id} value_basis"
            )
        normalized_navs.append(normalized_nav)
    if enforce_global_accounting_currency:
        if len(available_currencies) != 1:
            raise AccountAssignmentPerimeterError(
                "AVAILABLE NAV facts must use one accounting currency"
            )
        accounting_currency: str | None = next(iter(available_currencies))
    else:
        accounting_currency = None
    normalized_flows: list[dict[str, object]] = []
    flow_ids: set[str] = set()
    for index, row in enumerate(flows):
        if not isinstance(row, Mapping) or not {
            "record_id",
            "account_id",
            "event_date",
            "flow_type",
            "amount",
        } <= set(row):
            raise AccountAssignmentPerimeterError(
                "provider ledger flow shape is invalid"
            )
        record_id = _nonempty_text(row.get("record_id"), "flow record_id")
        if record_id in flow_ids or any(
            ord(item) < 32 or ord(item) == 127 for item in record_id
        ):
            raise AccountAssignmentPerimeterError(
                "provider ledger flow record_id is invalid"
            )
        flow_ids.add(record_id)
        account_id = _c3_account_id(row.get("account_id"), "flow account_id")
        event_date = _iso_date(row.get("event_date"), "flow event_date")
        _reject_provider_fact_after_ledger_date(event_date, ledger_as_of_date)
        nav_recognition_date = _iso_date(
            row.get("nav_recognition_date", event_date), "flow nav_recognition_date"
        )
        flow_type = _nonempty_text(row.get("flow_type"), "flow type")
        amount = _c3_money(
            row.get("amount"),
            f"flow {index}",
            allow_asset_currency=allow_asset_flow_currency,
        )
        if (
            enforce_global_accounting_currency
            and amount["status"] == "AVAILABLE"
            and amount["currency"] != accounting_currency
        ):
            raise AccountAssignmentPerimeterError(
                "AVAILABLE flow currency differs from accounting currency"
            )
        normalized_flow: dict[str, object] = {
            "record_id": record_id,
            "account_id": account_id,
            "event_date": event_date,
            "nav_recognition_date": nav_recognition_date,
            "flow_type": flow_type,
            "amount": amount,
        }
        normalized_flow.update(_c3_flow_scope_metadata(row, record_id))
        provider_id = row.get("provider_id")
        if provider_id is not None:
            normalized_flow["provider_id"] = _nonempty_text(
                provider_id, f"flow {record_id} provider_id"
            )
        generation_id = row.get("generation_id")
        if generation_id is not None:
            if (
                not isinstance(generation_id, str)
                or _SHA256.fullmatch(generation_id) is None
            ):
                raise AccountAssignmentPerimeterError(
                    f"flow {record_id} generation_id is invalid"
                )
            normalized_flow["generation_id"] = generation_id
        activity_id = row.get("provider_activity_id_sha256")
        if activity_id is not None:
            if (
                not isinstance(activity_id, str)
                or _SHA256.fullmatch(activity_id) is None
            ):
                raise AccountAssignmentPerimeterError(
                    f"flow {record_id} provider_activity_id_sha256 is invalid"
                )
            if (
                "provider_id" not in normalized_flow
                or "generation_id" not in normalized_flow
            ):
                raise AccountAssignmentPerimeterError(
                    f"flow {record_id} source identity is incomplete"
                )
            normalized_flow["provider_activity_id_sha256"] = activity_id
        normalized_flows.append(normalized_flow)
    normalized_anchors: list[dict[str, object]] = []
    anchor_ids: set[str] = set()
    required_anchor_fields = {
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
    for index, row in enumerate(anchors):
        if not isinstance(row, Mapping) or not required_anchor_fields <= set(row):
            raise AccountAssignmentPerimeterError(
                "provider capital anchor shape is invalid"
            )
        record_id = _nonempty_text(row.get("record_id"), "capital anchor record_id")
        if record_id in anchor_ids:
            raise AccountAssignmentPerimeterError(
                "duplicate provider capital anchor record_id"
            )
        anchor_ids.add(record_id)
        event_date = _iso_date(row.get("event_date"), "capital anchor event_date")
        recognition_date = _iso_date(
            row.get("nav_recognition_date"), "capital anchor nav_recognition_date"
        )
        if recognition_date < event_date:
            raise AccountAssignmentPerimeterError(
                "capital anchor recognition precedes its economic event"
            )
        _reject_provider_fact_after_ledger_date(event_date, ledger_as_of_date)
        capital_type = _nonempty_text(row.get("capital_type"), "capital anchor type")
        if capital_type not in {
            "OPENING_CAPITAL_ANCHOR",
            "STRATEGY_CAPITAL_ALLOCATION",
        }:
            raise AccountAssignmentPerimeterError(
                "provider capital anchor type is invalid"
            )
        if (
            row.get("mapping_status") != "MAPPED"
            or row.get("evidence_layer") != "ACCOUNT_LIVE"
        ):
            raise AccountAssignmentPerimeterError(
                "provider capital anchor mapping is unavailable"
            )
        normalized = dict(row)
        normalized.update(
            {
                "record_id": record_id,
                "account_id": _c3_account_id(
                    row.get("account_id"), "capital anchor account_id"
                ),
                "provider_id": _nonempty_text(
                    row.get("provider_id"), "capital anchor provider_id"
                ),
                "generation_id": _nonempty_text(
                    row.get("generation_id"), "capital anchor generation_id"
                ),
                "strategy_id": _nonempty_text(
                    row.get("strategy_id"), "capital anchor strategy_id"
                ),
                "event_date": event_date,
                "nav_recognition_date": recognition_date,
                "capital_type": capital_type,
                "amount": _c3_money(
                    row.get("amount"),
                    f"capital anchor {index} amount",
                    allow_asset_currency=True,
                ),
                "source_artifact_id": _nonempty_text(
                    row.get("source_artifact_id"), "capital anchor source_artifact_id"
                ),
                "source_record_id": _nonempty_text(
                    row.get("source_record_id"), "capital anchor source_record_id"
                ),
                "mapping_basis": _nonempty_text(
                    row.get("mapping_basis"), "capital anchor mapping_basis"
                ),
            }
        )
        if capital_type == "OPENING_CAPITAL_ANCHOR":
            if "opening_nav" not in row or "nav_source_artifact_id" not in row:
                raise AccountAssignmentPerimeterError(
                    "opening capital anchor lacks NAV provenance"
                )
            normalized["opening_nav"] = _c3_money(
                row.get("opening_nav"), f"capital anchor {index} opening_nav"
            )
            normalized["nav_source_artifact_id"] = _nonempty_text(
                row.get("nav_source_artifact_id"),
                "capital anchor nav_source_artifact_id",
            )
        else:
            if "opening_nav" in row:
                raise AccountAssignmentPerimeterError(
                    "capital allocation cannot declare opening NAV"
                )
            normalized["nav_source_artifact_id"] = _nonempty_text(
                row.get("nav_source_artifact_id"),
                "capital allocation nav_source_artifact_id",
            )
        normalized_anchors.append(normalized)
    normalized_ledger = dict(ledger)
    normalized_ledger["capital_anchors"] = normalized_anchors
    if sleeve is not None:
        _require_sleeve_reconciliation(normalized_navs, sleeve)
        _require_sleeve_references(normalized_flows, sleeve)
        if (
            {str(item["record_id"]) for item in sleeve.navs} & nav_ids
            or {str(item["record_id"]) for item in sleeve.flows} & flow_ids
            or any(
                item["account_id"] in sleeve.sleeve_accounts | sleeve.perimeter_accounts
                for item in (*normalized_navs, *normalized_flows)
            )
        ):
            raise AccountAssignmentPerimeterError(
                "the provider ledger already carries facts of a derived sleeve account"
            )
        normalized_navs = [*normalized_navs, *(dict(item) for item in sleeve.navs)]
        normalized_flows = [*normalized_flows, *(dict(item) for item in sleeve.flows)]
    return normalized_ledger, normalized_navs, normalized_flows, accounting_currency


def _mixed_projection_accounting_currency(
    navs: Sequence[Mapping[str, object]],
    flows: Sequence[Mapping[str, object]],
    capital_anchors: Sequence[Mapping[str, object]] = (),
) -> str:
    """Derive currency only from the exact admitted calculation projection."""

    available_currencies = {
        str(nav["nav"]["currency"])
        for nav in navs
        if isinstance(nav.get("nav"), Mapping)
        and nav["nav"].get("status") == "AVAILABLE"
    }
    if len(available_currencies) != 1:
        raise AccountAssignmentPerimeterError(
            "AVAILABLE NAV facts must use one accounting currency"
        )
    accounting_currency = next(iter(available_currencies))
    for flow in flows:
        amount = flow.get("amount")
        if (
            isinstance(amount, Mapping)
            and amount.get("status") == "AVAILABLE"
            and amount.get("currency") != accounting_currency
        ):
            raise AccountAssignmentPerimeterError(
                "AVAILABLE flow currency differs from accounting currency"
            )
    for anchor in capital_anchors:
        capital_type = anchor.get("capital_type")
        money = (
            anchor.get("opening_nav")
            if capital_type == "OPENING_CAPITAL_ANCHOR"
            else anchor.get("amount")
        )
        if (
            not isinstance(money, Mapping)
            or money.get("status") != "AVAILABLE"
            or money.get("currency") != accounting_currency
        ):
            raise AccountAssignmentPerimeterError(
                "capital anchor accounting currency differs from NAV currency"
            )
    return accounting_currency


def _c3_mapped_assignments(
    selected_inputs: AccountManifestSelectedInputs,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    baseline, providers, assignments, sleeve = _effective_perimeter(selected_inputs)
    for account_id in providers:
        _c3_account_id(account_id, "baseline account_id")
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            raise AccountAssignmentPerimeterError("baseline assignment is invalid")
        _c3_account_id(assignment.get("account_id"), "baseline assignment account_id")
    try:
        perimeter = partition_assignments(assignments)
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(
            "baseline assignments are invalid"
        ) from exc
    mapped = [
        dict(item) for chain in perimeter.mapped_chains.values() for item in chain
    ]
    for assignment in mapped:
        assignment["account_id"] = _c3_account_id(
            assignment["account_id"], "mapped assignment account_id"
        )
    mapped.sort(
        key=lambda item: (
            str(item["account_id"]),
            str(item["strategy_id"]),
            str(item["effective_start"]),
            str(item["effective_end"]),
        )
    )
    if not mapped:
        raise AccountAssignmentPerimeterError("baseline has no mapped assignments")
    if selected_inputs.current_epoch_crosswalk is not None:
        mapped = _validate_current_epoch_crosswalk(
            selected_inputs.baseline,
            selected_inputs.current_epoch_crosswalk,
            baseline_generation_id=selected_inputs.baseline_generation_id,
        )
        mapped = [
            {key: value for key, value in assignment.items() if key != "assignment_id"}
            for assignment in mapped
        ]
    if sleeve is not None:
        # The crosswalk names baseline epochs only: a residual parent's epochs
        # left the strategy set, the derived sleeves join it after them.
        mapped = [
            item for item in mapped if item["account_id"] not in sleeve.residual_parents
        ]
        mapped.extend(
            {key: value for key, value in item.items() if key != "assignment_id"}
            for item in sleeve.assignments
            if item["strategy_mapping_disposition"] == "STRATEGY_MAPPED"
        )
    tails = _tail_assignment_keys(selected_inputs)
    for assignment in mapped:
        if _tail_key(assignment) in tails:
            assignment["_evidence_open"] = True
    return mapped, providers


def _c3_assignment_record(assignment: Mapping[str, object]) -> dict[str, object]:
    return {
        key: assignment[key]
        for key in ("account_id", "strategy_id", "effective_start", "effective_end")
    }


def _c3_assignment_identity(mapped: list[dict[str, object]]) -> dict[str, object]:
    records = sorted(
        (_c3_assignment_record(item) for item in mapped),
        key=lambda item: (
            str(item["account_id"]),
            str(item["strategy_id"]),
            str(item["effective_start"]),
            str(item["effective_end"] or "9999-12-31"),
        ),
    )
    encoded = json.dumps(
        records, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "count": len(records)}


def _c3_in_interval(value: str, assignment: Mapping[str, object]) -> bool:
    # A tail epoch (#557) keeps its crosswalk end as identity; its evidence
    # window is open through the cutoff.
    end = None if assignment.get("_evidence_open") else assignment["effective_end"]
    return value >= str(assignment["effective_start"]) and (
        end is None or value <= str(end)
    )


def _c3_rule(
    rules: Mapping[str, object], flow_type: str
) -> Mapping[str, object] | None:
    exact = rules["exact"]
    assert isinstance(exact, Mapping)
    rule = exact.get(flow_type)
    if rule is not None:
        assert isinstance(rule, Mapping)
        return rule
    prefixes = rules["prefix"]
    assert isinstance(prefixes, list)
    matches = [
        item
        for item in prefixes
        if isinstance(item, Mapping)
        and flow_type.startswith(str(item["flow_type_prefix"]))
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise AccountAssignmentPerimeterError("flow classification rule is ambiguous")
    return matches[0]


def _c3_classified_flows(
    selected_inputs: AccountManifestSelectedInputs,
    ledger_identity: Mapping[str, object],
    flows: list[dict[str, object]],
    capital_anchors: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], InputSnapshot, dict[str, object], dict[str, object]]:
    try:
        policy_payload = selected_inputs.flow_classification_policy.json_object(
            "selected flow policy"
        )
    except ValueError:
        raise AccountAssignmentPerimeterError(
            "selected flow policy is invalid"
        ) from None
    policy_bytes = canonical_json_bytes(policy_payload)
    policy_snapshot = InputSnapshot.from_bytes("generated policy", policy_bytes)
    try:
        rules, policy_identity = load_flow_classification_policy(
            policy_payload, policy_snapshot
        )
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(
            "selected flow policy is invalid"
        ) from exc
    selected_rules = {
        str(flow["record_id"]): _c3_rule(rules, str(flow["flow_type"]))
        for flow in flows
    }

    def identity_compatible(
        flow: Mapping[str, object], item: Mapping[str, object]
    ) -> bool:
        rule = selected_rules[str(flow["record_id"])]
        if (
            rule is None
            or rule.get("counterparty_rule") != _SOURCE_IDENTITY_COUNTERPARTY_RULE
        ):
            return True
        activity_id = flow.get("provider_activity_id_sha256")
        other_activity_id = item.get("provider_activity_id_sha256")
        if activity_id is None and other_activity_id is None:
            return True
        return activity_id is not None and (
            flow.get("provider_id"),
            flow.get("generation_id"),
            activity_id,
        ) == (
            item.get("provider_id"),
            item.get("generation_id"),
            other_activity_id,
        )

    physical_candidates: dict[str, list[str]] = {}
    for flow in flows:
        record_id = str(flow["record_id"])
        amount = flow["amount"]
        assert isinstance(amount, Mapping)
        if amount["status"] != "AVAILABLE":
            physical_candidates[record_id] = []
            continue
        value = Decimal(str(amount["value"]))
        physical_candidates[record_id] = [
            str(item["record_id"])
            for item in flows
            if item["record_id"] != flow["record_id"]
            and item["event_date"] == flow["event_date"]
            and item["account_id"] != flow["account_id"]
            and isinstance(item["amount"], Mapping)
            and item["amount"].get("status") == "AVAILABLE"
            and item["amount"].get("currency") == amount["currency"]
            and Decimal(str(item["amount"].get("value"))) == -value
            and identity_compatible(flow, item)
        ]
    base_events: list[dict[str, object]] = []
    for flow in flows:
        flow_type = str(flow["flow_type"])
        rule = selected_rules[str(flow["record_id"])]
        amount = flow["amount"]
        assert isinstance(amount, Mapping)
        if rule is None:
            event = {
                "event_id": f"classified-flow:{flow['record_id']}",
                "source_record_id": flow["record_id"],
                "account_id": flow["account_id"],
                "event_date": flow["event_date"],
                "nav_recognition_date": flow["nav_recognition_date"],
                "flow_type": flow_type,
                "amount": amount,
                "classification_status": "BLOCKED",
                "disposition": None,
                "blocker_code": "FLOW_TYPE_UNRESOLVED",
            }
        elif amount["status"] == "UNAVAILABLE":
            event = {
                "event_id": f"classified-flow:{flow['record_id']}",
                "source_record_id": flow["record_id"],
                "account_id": flow["account_id"],
                "event_date": flow["event_date"],
                "nav_recognition_date": flow["nav_recognition_date"],
                "flow_type": flow_type,
                "amount": amount,
                "classification_status": "BLOCKED",
                "disposition": None,
                "blocker_code": "AMOUNT_UNAVAILABLE",
            }
        else:
            value = Decimal(str(amount["value"]))
            sign = rule["amount_sign"]
            if (
                (sign == "POSITIVE" and value <= 0)
                or (sign == "NEGATIVE" and value >= 0)
                or (sign == "NONZERO" and value == 0)
            ):
                event = {
                    "event_id": f"classified-flow:{flow['record_id']}",
                    "source_record_id": flow["record_id"],
                    "account_id": flow["account_id"],
                    "event_date": flow["event_date"],
                    "nav_recognition_date": flow["nav_recognition_date"],
                    "flow_type": flow_type,
                    "amount": amount,
                    "classification_status": "BLOCKED",
                    "disposition": None,
                    "blocker_code": "FLOW_AMOUNT_SIGN_MISMATCH",
                }
            else:
                event = {
                    "event_id": f"classified-flow:{flow['record_id']}",
                    "source_record_id": flow["record_id"],
                    "account_id": flow["account_id"],
                    "event_date": flow["event_date"],
                    "nav_recognition_date": flow["nav_recognition_date"],
                    "flow_type": flow_type,
                    "amount": amount,
                    "classification_status": "CLASSIFIED",
                    "disposition": rule["disposition"],
                    "blocker_code": None,
                }
        if capital_anchors is None:
            event.pop("nav_recognition_date")
        base_events.append(event)
    base_events_by_source_id = {
        str(item["source_record_id"]): item for item in base_events
    }
    events: list[dict[str, object]] = []
    for base_event in base_events:
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
        events.append(event)
    events.sort(key=lambda item: str(item["source_record_id"]))
    events_by_source_id = {str(item["source_record_id"]): item for item in events}
    for event in events:
        if event["classification_status"] != "CLASSIFIED":
            continue
        if event["disposition"] == "EXTERNAL":
            if "counterparty_source_record_id" in event:
                raise AccountAssignmentPerimeterError(
                    "external flow cannot have a counterparty"
                )
            continue
        counterparty_id = event.get("counterparty_source_record_id")
        counterpart = events_by_source_id.get(str(counterparty_id))
        if (
            not isinstance(counterpart, Mapping)
            or counterpart.get("classification_status") != "CLASSIFIED"
            or counterpart.get("disposition") != "INTERNAL"
            or counterpart.get("counterparty_source_record_id")
            != event["source_record_id"]
        ):
            raise AccountAssignmentPerimeterError(
                "internal flow must have exactly one reciprocal counterparty"
            )
    schema_version = 5 if capital_anchors is not None else 4
    payload = {
        "schema_version": schema_version,
        "artifact_id": "authority/classified-flows.json",
        "as_of_date": selected_inputs.generation_as_of_date.isoformat(),
        "ledger_identity": ledger_identity,
        "classification_policy": {
            "policy_id": policy_identity["policy_id"],
            "policy_version": policy_identity["policy_version"],
            "policy_as_of_date": policy_payload["as_of_date"],
            "policy_sha256": policy_snapshot.sha256,
        },
        "events": events,
    }
    if capital_anchors is not None:
        payload["capital_anchors"] = capital_anchors
    snapshot = InputSnapshot.from_bytes(
        "generated classified flows", canonical_json_bytes(payload)
    )
    return (
        payload,
        snapshot,
        _c3_artifact(
            snapshot,
            "authority/classified-flows.json",
            "classified-flows",
            selected_inputs.generation_as_of_date.isoformat(),
            str(schema_version),
        ),
        policy_identity,
    )


def build_blocked_account_manifest_payloads(
    selected_inputs: AccountManifestSelectedInputs,
) -> Mapping[str, bytes]:
    """Derive the closed, all-blocked nine-subject authority candidate."""
    if type(selected_inputs) is not AccountManifestSelectedInputs:
        raise AccountAssignmentPerimeterError(
            "selected_inputs must be an AccountManifestSelectedInputs"
        )
    if selected_inputs.receipt_authority is not None:
        raise AccountAssignmentPerimeterError(
            "mixed selected inputs require build_account_manifest_payloads"
        )
    _, navs, flows, accounting_currency = _c3_ledger(selected_inputs)
    mapped, providers = _c3_mapped_assignments(selected_inputs)
    assignment_identity = _c3_assignment_identity(mapped)
    selected_payload = json.loads(build_selected_input_identities(selected_inputs))
    ledger_source = {
        "generation_id": selected_payload["provider_ledger"]["generation_id"],
        "artifact": selected_payload["provider_ledger"]["artifact"],
        "identity_artifact": selected_payload["provider_ledger"]["generation_manifest"],
    }
    ledger_pair = {
        "generation_id": ledger_source["generation_id"],
        "artifact": ledger_source["artifact"],
    }
    baseline_source = {
        "generation_id": selected_payload["baseline"]["generation_id"],
        "artifact": selected_payload["baseline"]["artifact"],
    }
    nav_facts = [
        {
            "record_id": item["record_id"],
            "account_id": item["account_id"],
            "as_of_date": item["as_of_date"],
            "nav": {"status": item["nav"]["status"]},
        }
        for item in navs
    ]
    raw_facts = [
        {
            "record_id": item["record_id"],
            "account_id": item["account_id"],
            "event_date": item["event_date"],
        }
        for item in flows
    ]
    classified, classified_snapshot, classified_artifact, policy_identity = (
        _c3_classified_flows(selected_inputs, ledger_pair, flows)
    )
    classified_facts = [
        {
            "event_id": item["event_id"],
            "source_record_id": item["source_record_id"],
            "account_id": item["account_id"],
            "event_date": item["event_date"],
        }
        for item in classified["events"]
    ]
    perimeter_bytes = build_account_assignment_perimeter(
        selected_inputs=selected_inputs,
        nav_facts=nav_facts,
        raw_flow_facts=raw_facts,
        classified_flow_facts=classified_facts,
    )
    perimeter_snapshot = InputSnapshot.from_bytes(
        "generated perimeter", perimeter_bytes
    )
    policy_projection = canonical_json_bytes(
        selected_inputs.flow_classification_policy.json_object("selected flow policy")
    )
    policy_snapshot = InputSnapshot.from_bytes(
        "generated policy projection", policy_projection
    )
    policy_artifact = _c3_artifact(
        policy_snapshot,
        "authority/flow-classification-policy.json",
        "flow-classification-policy",
        str(
            selected_inputs.flow_classification_policy.json_object(
                "selected flow policy"
            )["as_of_date"]
        ),
    )
    coverage_rows: list[dict[str, object]] = []
    generated_date = selected_inputs.generation_as_of_date.isoformat()
    provider_ledger_date = _selected_top_level_date(
        selected_inputs.provider_ledger
    ).isoformat()
    for assignment in mapped:
        end = assignment["effective_end"] or provider_ledger_date
        if str(assignment["effective_start"]) > provider_ledger_date or (
            assignment["effective_end"] is not None
            and str(assignment["effective_end"]) > provider_ledger_date
        ):
            raise AccountAssignmentPerimeterError(
                "mapped assignment lies outside provider ledger date"
            )
        coverage_rows.append(
            {
                "assignment": _c3_assignment_record(assignment),
                "requested_start": assignment["effective_start"],
                "requested_end": end,
                "status": "BLOCKED",
                "authority_kind": "NONE",
                "receipt_artifact_id": None,
                "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT",
                "blocker_detail": "No admissible retained receipt issuer.",
            }
        )
    coverage_payload = {
        "schema_version": 2,
        "artifact_id": "authority/account-flow-coverage.json",
        "as_of_date": provider_ledger_date,
        "ledger_identity": ledger_pair,
        "classified_flows_artifact": classified_artifact,
        "mapped_assignments_identity": assignment_identity,
        "receipt_artifacts": [],
        "rows": coverage_rows,
    }
    coverage_snapshot = InputSnapshot.from_bytes(
        "generated blocked coverage", canonical_json_bytes(coverage_payload)
    )
    try:
        load_flow_coverage(
            coverage_snapshot, ledger_pair, classified_artifact, {}, mapped
        )
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(
            "generated blocked coverage is invalid"
        ) from exc
    coverage_artifact = _c3_artifact(
        coverage_snapshot,
        "authority/account-flow-coverage.json",
        "account-flow-coverage",
        provider_ledger_date,
        "2",
    )
    calculation_rows = [
        {
            "epoch": _c3_assignment_record(assignment),
            "provider": providers[str(assignment["account_id"])],
            "nav_observations": [
                item
                for item in navs
                if item["account_id"] == assignment["account_id"]
                and _c3_in_interval(str(item["as_of_date"]), assignment)
            ],
            "classified_flows": [
                item
                for item in classified["events"]
                if item["account_id"] == assignment["account_id"]
                and _c3_in_interval(str(item["event_date"]), assignment)
            ],
        }
        for assignment in mapped
    ]
    calculation_payload = {
        "schema_version": 1,
        "artifact_id": "authority/calculation-inputs.json",
        "as_of_date": generated_date,
        "accounting_currency": accounting_currency,
        "ledger_identity": ledger_source,
        "baseline_identity": baseline_source,
        "mapped_assignments_identity": assignment_identity,
        "rows": calculation_rows,
    }
    calculation_snapshot = InputSnapshot.from_bytes(
        "generated calculation inputs",
        canonical_json_bytes(json_ready(calculation_payload)),
    )
    calculation_artifact = _c3_artifact(
        calculation_snapshot,
        "authority/calculation-inputs.json",
        "account-manifest-calculation-inputs",
        generated_date,
    )
    admission_rows = [
        {
            "assignment": _c3_assignment_record(assignment),
            "status": "BLOCKED",
            "authority_kind": "NONE",
            "funded_nav_record_ids": [],
            "continuity_to_successor": False,
            "blocker_code": "FLOW_COMPLETENESS_NOT_PROVEN",
        }
        for assignment in mapped
    ]
    admission_payload = {
        "schema_version": 1,
        "artifact_id": "authority/mapped-epoch-admission.json",
        "as_of_date": generated_date,
        "ledger_identity": ledger_pair,
        "coverage_artifact": coverage_artifact,
        "mapped_assignments_identity": assignment_identity,
        "rows": admission_rows,
    }
    admission_snapshot = InputSnapshot.from_bytes(
        "generated admission", canonical_json_bytes(admission_payload)
    )
    admission_artifact = _c3_artifact(
        admission_snapshot,
        "authority/mapped-epoch-admission.json",
        "mapped-epoch-admission",
        generated_date,
    )
    code_payload = {
        "schema_version": 1,
        "artifact_id": "authority/code-identities.json",
        "as_of_date": generated_date,
        "code_sources": selected_payload["code_sources"],
    }
    code_snapshot = InputSnapshot.from_bytes(
        "generated code identities", canonical_json_bytes(code_payload)
    )
    code_artifact = _c3_artifact(
        code_snapshot,
        "authority/code-identities.json",
        "account-manifest-code-identities",
        generated_date,
    )
    output_rows = [
        {
            "account_id": assignment["account_id"],
            "provider": providers[str(assignment["account_id"])],
            "strategy_id": assignment["strategy_id"],
            "effective_start": assignment["effective_start"],
            "effective_end": assignment["effective_end"],
            "admission_status": "BLOCKED",
            "admission_policy_reference": "NONE",
            "admission_authority_kind": "NONE",
            "funded_nav_record_ids": [],
            "continuity_to_successor": False,
            "status": "BLOCKED_ADMISSION",
            "blocking_reason": "FLOW_COMPLETENESS_NOT_PROVEN",
        }
        for assignment in mapped
    ]
    output_payload = {
        "schema_version": 8,
        "artifact": "account-strategy-manifest",
        "accounting_currency": accounting_currency,
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
                    "policy_id": policy_identity["policy_id"],
                    "policy_version": policy_identity["policy_version"],
                    "policy_as_of_date": selected_inputs.flow_classification_policy.json_object(
                        "selected flow policy"
                    )["as_of_date"],
                    "policy_sha256": policy_snapshot.sha256,
                },
            },
            "flow_classification_policy": {
                **policy_identity,
                "artifact": policy_artifact,
            },
            "flow_coverage": {
                "artifact": coverage_artifact,
                "ledger_identity": ledger_source,
                "classified_flows_artifact": classified_artifact,
            },
            "admission_policy": {
                "policy_id": "MAPPED_EPOCH_ADMISSION_V1",
                "policy_version": "1",
                "artifact": admission_artifact,
                "assignment": assignment_identity,
            },
            "assignment": assignment_identity,
            "calculation_code": selected_payload["code_sources"]["epoch_producer"],
            "selected_inputs": _c3_artifact(
                InputSnapshot.from_bytes(
                    "selected identities", canonical_json_bytes(selected_payload)
                ),
                "authority/selected-input-identities.json",
                "selected-input-identities",
                generated_date,
            ),
            "account_assignment_perimeter": _c3_artifact(
                perimeter_snapshot,
                "authority/account-assignment-perimeter.json",
                "account-assignment-perimeter",
                generated_date,
            ),
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
    payloads = {
        "authority/selected-input-identities.json": canonical_json_bytes(
            selected_payload
        ),
        "authority/account-assignment-perimeter.json": perimeter_bytes,
        "authority/calculation-inputs.json": calculation_snapshot.contents,
        "authority/flow-classification-policy.json": policy_projection,
        "authority/classified-flows.json": classified_snapshot.contents,
        "authority/account-flow-coverage.json": coverage_snapshot.contents,
        "authority/mapped-epoch-admission.json": admission_snapshot.contents,
        "authority/code-identities.json": code_snapshot.contents,
        "outputs/account-strategy-manifest.json": canonical_json_bytes(output_payload),
    }
    return MappingProxyType(dict(payloads))


def validate_blocked_account_manifest_payloads(
    payloads: Mapping[str, bytes], selected_inputs: AccountManifestSelectedInputs
) -> None:
    if not isinstance(payloads, Mapping) or dict(payloads) != dict(
        build_blocked_account_manifest_payloads(selected_inputs)
    ):
        raise AccountAssignmentPerimeterError(
            "blocked account manifest payloads differ from expected derivation"
        )


def build_account_manifest_payloads(
    selected_inputs: AccountManifestSelectedInputs,
) -> Mapping[str, bytes]:
    """Build legacy blocked bytes or the selected receipt-authorized mixed candidate."""
    if type(selected_inputs) is not AccountManifestSelectedInputs:
        raise AccountAssignmentPerimeterError(
            "selected_inputs must be an AccountManifestSelectedInputs"
        )
    if selected_inputs.receipt_authority is None:
        return build_blocked_account_manifest_payloads(selected_inputs)
    if selected_inputs.current_epoch_crosswalk is None:
        raise AccountAssignmentPerimeterError(
            "mixed current-product generation requires current_epoch_crosswalk"
        )
    return _build_mixed_account_manifest_payloads(selected_inputs)


def _validate_mixed_receipt_cardinality(
    receipts: Mapping[object, object],
) -> None:
    """Enforce the closed mixed route-table population boundary."""

    if not isinstance(receipts, Mapping) or not 1 <= len(receipts) <= 15:
        raise AccountAssignmentPerimeterError(
            "mixed composition requires between 1 and 15 receipts"
        )


def _tail_key(assignment: Mapping[str, object]) -> tuple[str, str, str, str | None]:
    end = assignment["effective_end"]
    return (
        str(assignment["account_id"]),
        str(assignment["strategy_id"]),
        str(assignment["effective_start"]),
        None if end is None else str(end),
    )


def _tail_assignment_keys(
    selected_inputs: "AccountManifestSelectedInputs",
    mapped: Sequence[Mapping[str, object]] | None = None,
) -> set[tuple[str, str, str, str | None]]:
    """Final closed epochs whose admission declares a funded end.

    The return closes at the funded end; the evidence window (receipt,
    coverage, facts) runs through the cutoff so the post-end tail stays inside
    the same strategy epoch (#557).  Only the last row of an account's full
    baseline chain qualifies: a later row owns the facts that follow.
    """

    del mapped
    if selected_inputs.admission_policy is None:
        return set()
    try:
        policy = selected_inputs.admission_policy.json_object("admission policy")
        baseline = selected_inputs.baseline.json_object("selected baseline")
    except ValueError:
        return set()
    admissions = policy.get("admissions")
    assignments = baseline.get("assignments")
    if not isinstance(admissions, list) or not isinstance(assignments, list):
        return set()
    declared = {
        _tail_key(row)
        for row in admissions
        if isinstance(row, Mapping)
        and row.get("status") == "FUNDED"
        and row.get("funded_end") is not None
        and row.get("effective_end") is not None
    }
    last: dict[str, Mapping[str, object]] = {}
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            continue
        account = str(assignment.get("account_id"))
        current = last.get(account)
        if current is None or str(assignment.get("effective_start") or "") > str(
            current.get("effective_start") or ""
        ):
            last[account] = assignment
    return {
        _tail_key(item)
        for item in last.values()
        if item.get("strategy_mapping_disposition") == "STRATEGY_MAPPED"
        and item.get("effective_end") is not None
    } & declared


def _mixed_receipt_routes(
    selected_inputs: AccountManifestSelectedInputs,
    mapped: list[dict[str, object]],
    residual_parents: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Route every selected receipt to one distinct mapped interval."""
    _validate_mixed_receipt_cardinality(selected_inputs.retrieval_receipts)
    authority = selected_inputs.receipt_authority
    assert authority is not None
    try:
        authority_payload = authority.json_object("selected receipt authority")
    except ValueError:
        raise AccountAssignmentPerimeterError(
            "selected receipt authority is invalid"
        ) from None
    entries = authority_payload.get("receipts")
    if not isinstance(entries, list) or not entries:
        raise AccountAssignmentPerimeterError("selected receipt authority is invalid")
    try:
        authority_date = _iso_date(
            authority_payload.get("as_of_date"),
            "selected receipt authority as_of_date",
        )
    except (TypeError, ValueError):
        raise AccountAssignmentPerimeterError(
            "selected receipt authority is invalid"
        ) from None
    authority_ids = [
        entry.get("artifact_id") if isinstance(entry, Mapping) else None
        for entry in entries
    ]
    if (
        any(not isinstance(receipt_id, str) for receipt_id in authority_ids)
        or authority_ids != sorted(authority_ids, key=str.casefold)
        or len({str(receipt_id).casefold() for receipt_id in authority_ids})
        != len(authority_ids)
        or set(authority_ids) != set(selected_inputs.retrieval_receipts)
    ):
        raise AccountAssignmentPerimeterError(
            "mixed composition receipt selection differs from authority"
        )
    provider_ledger_date = _selected_top_level_date(
        selected_inputs.provider_ledger
    ).isoformat()
    if authority_date > provider_ledger_date:
        raise AccountAssignmentPerimeterError(
            "selected receipt does not route to exactly one mapped assignment"
        )
    routes: list[dict[str, object]] = []
    routed_keys: set[tuple[object, ...]] = set()
    tail_keys = _tail_assignment_keys(selected_inputs, mapped)
    for receipt_id in authority_ids:
        assert isinstance(receipt_id, str)
        receipt = selected_inputs.retrieval_receipts[receipt_id]
        try:
            payload = receipt.json_object("selected retrieval receipt")
        except ValueError:
            raise AccountAssignmentPerimeterError(
                "selected retrieval receipt is invalid"
            ) from None
        account_id = payload.get("account_id")
        start, end = payload.get("requested_start"), payload.get("requested_end")
        try:
            receipt_end = _iso_date(end, "receipt requested_end")
        except (TypeError, ValueError):
            raise AccountAssignmentPerimeterError(
                "selected receipt does not route to exactly one mapped assignment"
            ) from None
        if receipt_end > provider_ledger_date:
            raise AccountAssignmentPerimeterError(
                "selected receipt does not route to exactly one mapped assignment"
            )
        matches = [
            assignment
            for assignment in mapped
            if assignment["account_id"] == account_id
            and assignment["effective_start"] == start
            and (
                assignment["effective_end"] is None
                or assignment["effective_end"] == end
                # A final closed epoch whose admission declares a funded end
                # carries its evidence through the cutoff (#557).
                or (
                    end == provider_ledger_date
                    and tail_keys.issuperset({_tail_key(assignment)})
                )
            )
        ]
        if not matches and account_id in residual_parents:
            # A residual parent is no longer a strategy epoch: its receipt is the
            # flow-completeness evidence its derived sleeves cite.
            routes.append(
                {"receipt_id": receipt_id, "receipt": payload, "assignment": None}
            )
            continue
        if len(matches) != 1:
            raise AccountAssignmentPerimeterError(
                "selected receipt does not route to exactly one mapped assignment"
            )
        route_key = tuple(_c3_assignment_record(matches[0]).values())
        if route_key in routed_keys:
            raise AccountAssignmentPerimeterError(
                "selected receipts route to a duplicate mapped assignment"
            )
        routed_keys.add(route_key)
        routes.append(
            {
                "receipt_id": receipt_id,
                "receipt": payload,
                "assignment": matches[0],
            }
        )
    return routes


def _sleeve_display_by_epoch(
    sleeve: SleeveManifestFacts,
    display_by_epoch: Mapping[tuple[object, object, object, object], dict[str, object]],
) -> dict[tuple[object, object, object, object], dict[str, object]]:
    """Display fields of derived epochs: the parent's label plus the sleeve id."""

    labels: dict[str, dict[str, object]] = {}
    for key, value in display_by_epoch.items():
        labels.setdefault(str(key[0]), value)
    result: dict[tuple[object, object, object, object], dict[str, object]] = {}
    for derived in sleeve.coverage:
        record = derived["assignment"]
        parent = str(derived["parent_account_id"])
        parent_label = labels.get(parent)
        if parent_label is None:
            raise AccountAssignmentPerimeterError(
                f"parent account {parent} has no crosswalk label for its sleeves"
            )
        suffix = str(record["account_id"]).removeprefix(f"{parent}-")
        result[tuple(record.values())] = {
            "account_label": f"{parent_label['account_label']} / {suffix}",
            "last_four": parent_label["last_four"],
        }
    return result


def _sleeve_parent_route(
    derived: Mapping[str, object], routes: Sequence[Mapping[str, object]]
) -> Mapping[str, object]:
    """The one selected receipt of the parent that covers a derived epoch."""

    matches = [
        route
        for route in routes
        if route["receipt"].get("account_id") == derived["parent_account_id"]
        and str(route["receipt"]["requested_start"]) <= str(derived["requested_start"])
        and str(route["receipt"]["requested_end"]) >= str(derived["requested_end"])
    ]
    if len(matches) != 1:
        record = derived["assignment"]
        raise AccountAssignmentPerimeterError(
            f"derived epoch {record['account_id']} "
            f"{derived['requested_start']}..{derived['requested_end']} is covered "
            f"by {len(matches)} selected receipts of parent "
            f"{derived['parent_account_id']}, not exactly one"
        )
    return matches[0]


def _sleeve_calculation_routes(
    sleeve: SleeveManifestFacts | None,
    mapped: Sequence[Mapping[str, object]],
    routes: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Routes of the calculable epochs: receipt-routed ones plus derived sleeves."""

    result: list[dict[str, object]] = [
        dict(route) for route in routes if route["assignment"] is not None
    ]
    if sleeve is None:
        return result
    mapped_by_key = {
        tuple(_c3_assignment_record(item).values()): item for item in mapped
    }
    for derived in sleeve.coverage:
        assignment = mapped_by_key.get(tuple(derived["assignment"].values()))
        if assignment is None:
            continue
        parent_route = _sleeve_parent_route(derived, routes)
        result.append(
            {
                "receipt_id": parent_route["receipt_id"],
                "receipt": {
                    "requested_start": derived["requested_start"],
                    "requested_end": derived["requested_end"],
                },
                "assignment": assignment,
                "derived": True,
            }
        )
    return result


def _effective_admission_payload(
    selected_inputs: AccountManifestSelectedInputs,
    sleeve: SleeveManifestFacts | None,
) -> dict[str, object]:
    """The selected admission policy plus the derived sleeve admissions."""

    assert selected_inputs.admission_policy is not None
    payload = selected_inputs.admission_policy.json_object("selected admission policy")
    if sleeve is None:
        return payload
    rows = payload.get("admissions")
    if not isinstance(rows, list):
        raise AccountAssignmentPerimeterError("selected admission policy is invalid")
    kept = [
        row
        for row in rows
        if not (
            isinstance(row, Mapping)
            and row.get("account_id") in sleeve.residual_parents
        )
    ]
    payload = dict(payload)
    payload["admissions"] = [*kept, *(dict(row) for row in sleeve.admissions)]
    return payload


def _mixed_coverage_payload(
    *,
    selected_inputs: AccountManifestSelectedInputs,
    mapped: list[dict[str, object]],
    ledger_identity: Mapping[str, object],
    classified_artifact: Mapping[str, object],
    assignment_identity: Mapping[str, object],
    routes: Sequence[Mapping[str, object]],
    sleeve: SleeveManifestFacts | None = None,
) -> dict[str, object]:
    provider_ledger_date = _selected_top_level_date(
        selected_inputs.provider_ledger
    ).isoformat()
    route_by_assignment = {
        tuple(_c3_assignment_record(route["assignment"]).values()): route
        for route in routes
        if route["assignment"] is not None
    }
    derived_by_key = {
        tuple(row["assignment"].values()): row
        for row in ([] if sleeve is None else sleeve.coverage)
    }
    rows: list[dict[str, object]] = []
    for assignment in mapped:
        derived = derived_by_key.get(tuple(_c3_assignment_record(assignment).values()))
        if derived is not None:
            parent_route = _sleeve_parent_route(derived, routes)
            rows.append(
                {
                    "assignment": _c3_assignment_record(assignment),
                    "requested_start": derived["requested_start"],
                    "requested_end": derived["requested_end"],
                    "status": "COMPLETE",
                    "authority_kind": DERIVED_SLEEVE_COVERAGE_KIND,
                    "receipt_artifact_id": parent_route["receipt_id"],
                    "blocker_code": None,
                    "blocker_detail": None,
                    "derivation": {
                        "parent_account_id": derived["parent_account_id"],
                        "sleeve_facts_sha256": derived["sleeve_facts_sha256"],
                        "tag_map_sha256": derived["tag_map_sha256"],
                    },
                }
            )
            continue
        route = route_by_assignment.get(
            tuple(_c3_assignment_record(assignment).values())
        )
        complete = route is not None
        rows.append(
            {
                "assignment": _c3_assignment_record(assignment),
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
                "blocker_detail": None
                if complete
                else "No admissible retained receipt issuer.",
            }
        )
    return {
        "schema_version": 2,
        "artifact_id": "authority/account-flow-coverage.json",
        "as_of_date": provider_ledger_date,
        "ledger_identity": dict(ledger_identity),
        "classified_flows_artifact": dict(classified_artifact),
        "mapped_assignments_identity": dict(assignment_identity),
        "receipt_artifacts": [
            {
                "artifact_id": str(route["receipt_id"]),
                "sha256": selected_inputs.retrieval_receipts[
                    str(route["receipt_id"])
                ].sha256,
                "size_bytes": selected_inputs.retrieval_receipts[
                    str(route["receipt_id"])
                ].size_bytes,
            }
            for route in routes
        ],
        "rows": rows,
    }


def _validate_mixed_receipt_provider_evidence(
    selected_inputs: AccountManifestSelectedInputs,
    receipt: Mapping[str, object],
    baseline_provider: object,
) -> None:
    """Bind the selected receipt to one selected-ledger provider generation."""
    try:
        ledger = selected_inputs.provider_ledger.json_object("selected provider ledger")
    except ValueError:
        raise AccountAssignmentPerimeterError(
            "selected receipt provider evidence differs"
        ) from None
    ledger_as_of_date = _selected_top_level_date(selected_inputs.provider_ledger)
    provider = receipt.get("provider")
    if not isinstance(provider, str) or not isinstance(baseline_provider, str):
        raise AccountAssignmentPerimeterError(
            "selected receipt provider evidence differs"
        )
    generations = ledger.get("provider_generations")
    if not isinstance(generations, list):
        raise AccountAssignmentPerimeterError(
            "selected receipt provider evidence differs"
        )
    if receipt.get("coverage_mode") == "STATIC_SNAPSHOT_COVERAGE_V1":
        required = {
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
            set(receipt) != required
            or receipt.get("schema_version") != 3
            or receipt.get("date_convention") != "EOD_PROVIDER_NAV_RECOGNITION_DATE"
            or receipt.get("completeness") != "RETAINED_STATIC_SNAPSHOT_ONLY"
        ):
            raise AccountAssignmentPerimeterError("selected static receipt is invalid")
        generation = receipt.get("provider_generation")
        staged = receipt.get("staged_inputs")
        if not isinstance(generation, Mapping) or not isinstance(staged, Mapping):
            raise AccountAssignmentPerimeterError("selected static receipt is invalid")
        matches = [
            row
            for row in generations
            if isinstance(row, Mapping)
            and row.get("provider_id") == str(baseline_provider).lower()
            and receipt.get("provider") == str(baseline_provider)
            and dict(generation)
            == {
                "generation_id": row.get("generation_id"),
                "catalog_sha256": row.get("catalog_sha256"),
                "manifest_sha256": row.get("manifest_sha256"),
                "generation_as_of_date": row.get("generation_as_of_date"),
            }
        ]
        if len(matches) != 1 or selected_inputs.current_epoch_crosswalk is None:
            raise AccountAssignmentPerimeterError(
                "selected static receipt provider evidence differs"
            )
        expected_staged = {
            "provider_ledger_generation_id": selected_inputs.provider_ledger_generation_id,
            "provider_ledger_artifact": _bound_catalog_identity(
                catalog=selected_inputs.provider_ledger,
                manifest_snapshot=selected_inputs.provider_ledger_manifest,
                catalog_artifact_id=_PROVIDER_LEDGER_ARTIFACT_ID,
                catalog_schema_name=_PROVIDER_LEDGER_SCHEMA_NAME,
                catalog_schema_version_field="catalog_schema_version",
                manifest_identity_artifact_id="identity/provider-ledger-generation.json",
            )[1]["artifact"],
            "baseline_sha256": selected_inputs.baseline.sha256,
            "baseline_size_bytes": selected_inputs.baseline.size_bytes,
            "crosswalk_sha256": selected_inputs.current_epoch_crosswalk.sha256,
            "crosswalk_size_bytes": selected_inputs.current_epoch_crosswalk.size_bytes,
        }
        if dict(staged) != expected_staged:
            raise AccountAssignmentPerimeterError(
                "selected static receipt staged inputs differ"
            )
        start, end = (
            _iso_date(receipt.get("requested_start"), "static receipt start"),
            _iso_date(receipt.get("requested_end"), "static receipt end"),
        )
        if (
            receipt.get("calculation_cutoff_date")
            != _current_epoch_calculation_cutoff(selected_inputs).isoformat()
        ):
            raise AccountAssignmentPerimeterError(
                "selected static receipt cutoff differs"
            )
        receipt_cutoff = _iso_date(
            receipt.get("calculation_cutoff_date"),
            "static receipt calculation cutoff",
        )
        receipt_coverage = _iso_date(
            receipt.get("coverage_as_of_date"),
            "static receipt coverage as_of_date",
        )
        if (
            start > end
            or end > ledger_as_of_date.isoformat()
            or receipt_cutoff > ledger_as_of_date.isoformat()
            or receipt_coverage != ledger_as_of_date.isoformat()
        ):
            raise AccountAssignmentPerimeterError(
                "selected static receipt financial interval exceeds ledger date"
            )

        def fact_date(row: Mapping[str, object], kind: str) -> str:
            return str(
                row.get("as_of_date")
                if kind == "nav"
                else row.get("nav_recognition_date", row.get("event_date"))
            )

        expected_facts = {}
        for kind, field in (
            ("nav", "nav_observations"),
            ("flows", "flows"),
            ("capital_anchors", "capital_anchors"),
        ):
            rows = [
                row
                for row in ledger.get(field, [])
                if isinstance(row, Mapping)
                and row.get("account_id") == receipt.get("account_id")
                and (
                    kind != "capital_anchors"
                    or row.get("strategy_id") == receipt.get("strategy_id")
                )
                and not (
                    kind == "flows"
                    and row.get("provider_account_scope") == "ACCOUNT_ONLY"
                    and row.get("mapping_status") == "UNMAPPED"
                    and "strategy_id" in row
                    and row["strategy_id"] is None
                )
                and start <= fact_date(row, kind) <= end
            ]
            dates = sorted(fact_date(row, kind) for row in rows)
            expected_facts[kind] = {
                "row_count": len(rows),
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
            }
        if receipt.get("observed_facts") != expected_facts:
            raise AccountAssignmentPerimeterError(
                "selected static receipt observed facts differ"
            )
        return
    manifest = receipt.get("provider_generation_manifest")
    matches = []
    for row in generations:
        if not isinstance(row, Mapping):
            continue
        provider_id = row.get("provider_id")
        if (
            not isinstance(provider_id, str)
            or _INTERNAL_ACCOUNT_ID.fullmatch(provider_id) is None
            or baseline_provider != provider_id.upper()
            or provider != provider_id.upper()
        ):
            continue
        if (
            isinstance(manifest, Mapping)
            and receipt.get("provider_generation") == row.get("generation_id")
            and manifest.get("sha256") == row.get("manifest_sha256")
            and manifest.get("as_of_date") == row.get("generation_as_of_date")
        ):
            matches.append(row)
    if len(matches) != 1:
        raise AccountAssignmentPerimeterError(
            "selected receipt provider evidence differs"
        )
    generation = matches[0]
    _iso_date(generation.get("generation_as_of_date"), "provider generation as_of_date")
    if isinstance(manifest, Mapping):
        _iso_date(
            manifest.get("as_of_date"),
            "provider generation manifest as_of_date",
        )
    raw_artifacts = receipt.get("raw_evidence_artifacts")
    if isinstance(raw_artifacts, list):
        for artifact in raw_artifacts:
            if isinstance(artifact, Mapping):
                _iso_date(
                    artifact.get("as_of_date"),
                    "raw evidence artifact as_of_date",
                )
    for field_name in (
        "requested_start",
        "requested_end",
        "covered_start",
        "covered_end",
    ):
        if field_name in receipt:
            receipt_date = _iso_date(receipt[field_name], f"receipt {field_name}")
            _reject_provider_fact_after_ledger_date(receipt_date, ledger_as_of_date)
    if (
        not isinstance(manifest, Mapping)
        or receipt.get("provider_generation") != generation.get("generation_id")
        or manifest.get("sha256") != generation.get("manifest_sha256")
        or manifest.get("as_of_date") != generation.get("generation_as_of_date")
    ):
        raise AccountAssignmentPerimeterError(
            "selected receipt provider evidence differs"
        )


def _mixed_admitted_fact_projection(
    *,
    funded_assignments: Sequence[Mapping[str, object]],
    routes: Sequence[Mapping[str, object]],
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
    """Select every funded epoch and its exact internal support legs."""

    funded_keys = {
        tuple(_c3_assignment_record(assignment).values())
        for assignment in funded_assignments
    }

    funded_routes = [
        route
        for route in routes
        if tuple(_c3_assignment_record(route["assignment"]).values()) in funded_keys
    ]

    def in_funded_epoch(fact: Mapping[str, object], date_field: str) -> bool:
        return any(
            fact.get("account_id") == route["assignment"]["account_id"]
            and route["receipt"]["requested_start"]
            <= str(fact.get(date_field))
            <= route["receipt"]["requested_end"]
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
        if in_funded_epoch(event, "nav_recognition_date")
        and str(event.get("source_record_id")) not in excluded_account_only_source_ids
    ]
    if any(event.get("classification_status") != "CLASSIFIED" for event in root_events):
        raise AccountAssignmentPerimeterError(
            "receipt-routed epoch contains a blocked classified flow"
        )
    events_by_source_id = {
        str(event.get("source_record_id")): event for event in events
    }
    supporting_ids: set[str] = set()
    for event in root_events:
        if event.get("disposition") == "INTERNAL":
            counterparty_id = event.get("counterparty_source_record_id")
            counterpart = events_by_source_id.get(str(counterparty_id))
            if (
                not isinstance(counterparty_id, str)
                or not isinstance(counterpart, Mapping)
                or counterpart.get("classification_status") != "CLASSIFIED"
                or counterpart.get("disposition") != "INTERNAL"
                or counterpart.get("counterparty_source_record_id")
                != event.get("source_record_id")
            ):
                raise AccountAssignmentPerimeterError(
                    "receipt-routed internal flow support is invalid"
                )
            supporting_ids.add(counterparty_id)
        elif event.get("disposition") != "EXTERNAL":
            raise AccountAssignmentPerimeterError(
                "receipt-routed classified flow disposition is invalid"
            )
    projected_source_ids = {
        str(event["source_record_id"]) for event in root_events
    } | supporting_ids
    projected_events = [
        event
        for event in events
        if str(event.get("source_record_id")) in projected_source_ids
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
        raise AccountAssignmentPerimeterError(
            "receipt-routed calculation projection is incomplete"
        )
    # R1: each funded epoch opens at its account's last observation before the
    # funded start, so that one observation (of any status) is projected too.
    opening_record_ids: set[object] = set()
    for route in funded_routes:
        route_account = route["assignment"]["account_id"]
        route_start = str(route["assignment"]["effective_start"])
        prior = [
            nav
            for nav in navs
            if nav.get("account_id") == route_account
            and str(nav.get("as_of_date")) < route_start
        ]
        if prior:
            opening_record_ids.add(
                max(prior, key=lambda nav: str(nav.get("as_of_date"))).get("record_id")
            )
    projected_navs = [
        nav
        for nav in navs
        if in_funded_epoch(nav, "as_of_date")
        or nav.get("record_id") in opening_record_ids
    ]

    def anchor_membership_date(anchor: Mapping[str, object]) -> str:
        if anchor.get("capital_type") == "STRATEGY_CAPITAL_ALLOCATION":
            return str(anchor["event_date"])
        return str(anchor.get("nav_recognition_date", anchor.get("event_date")))

    projected_anchors = [
        anchor
        for anchor in capital_anchors
        if any(
            anchor.get("account_id") == route["assignment"]["account_id"]
            and anchor.get("strategy_id") == route["assignment"]["strategy_id"]
            and _c3_in_interval(anchor_membership_date(anchor), route["assignment"])
            and route["receipt"]["requested_start"]
            <= anchor_membership_date(anchor)
            <= route["receipt"]["requested_end"]
            for route in funded_routes
        )
    ]
    descriptor = {
        "selector": "ALL_FUNDED_EPOCHS_V1",
        "funded_epochs": [
            _c3_assignment_record(route["assignment"]) for route in funded_routes
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


def _mixed_funded_assignments(
    selected_inputs: AccountManifestSelectedInputs,
    mapped: Sequence[Mapping[str, object]],
    sleeve: SleeveManifestFacts | None = None,
) -> list[Mapping[str, object]]:
    """Read the funded subset needed to construct the producer projection."""

    assert selected_inputs.admission_policy is not None
    try:
        policy = _effective_admission_payload(selected_inputs, sleeve)
    except ValueError:
        raise AccountAssignmentPerimeterError(
            "selected admission policy is invalid"
        ) from None
    admissions = policy.get("admissions")
    if not isinstance(admissions, list):
        raise AccountAssignmentPerimeterError("selected admission policy is invalid")
    funded_keys: set[tuple[object, ...]] = set()
    for row in admissions:
        if not isinstance(row, Mapping) or row.get("status") != "FUNDED":
            continue
        key = tuple(
            row.get(field)
            for field in (
                "account_id",
                "strategy_id",
                "effective_start",
                "effective_end",
            )
        )
        if key in funded_keys:
            raise AccountAssignmentPerimeterError(
                "selected admission policy contains duplicate funded epochs"
            )
        funded_keys.add(key)
    funded = [
        assignment
        for assignment in mapped
        if tuple(_c3_assignment_record(assignment).values()) in funded_keys
    ]
    if len(funded) != len(funded_keys):
        raise AccountAssignmentPerimeterError(
            "selected funded admission does not match the mapped perimeter"
        )
    return funded


def _build_mixed_account_manifest_payloads(
    selected_inputs: AccountManifestSelectedInputs,
) -> Mapping[str, bytes]:
    """Compose one receipt-authorized C3 candidate through the public producer."""
    assert selected_inputs.receipt_authority is not None
    assert selected_inputs.admission_policy is not None
    assert selected_inputs.manifest_schema is not None
    sleeve = _sleeve_manifest_facts(selected_inputs)
    normalized_ledger, navs, flows, _ = _c3_ledger(
        selected_inputs,
        enforce_global_accounting_currency=False,
        allow_asset_flow_currency=True,
        sleeve=sleeve,
    )
    anchors = normalized_ledger["capital_anchors"]
    assert isinstance(anchors, list)
    lineage_navs = navs
    lineage_flows = flows
    mapped, providers = _c3_mapped_assignments(selected_inputs)
    assignment_identity = _c3_assignment_identity(mapped)
    selected_payload = json.loads(build_selected_input_identities(selected_inputs))
    ledger_source = {
        "generation_id": selected_payload["provider_ledger"]["generation_id"],
        "artifact": selected_payload["provider_ledger"]["artifact"],
        "identity_artifact": selected_payload["provider_ledger"]["generation_manifest"],
    }
    ledger_pair = {
        "generation_id": ledger_source["generation_id"],
        "artifact": ledger_source["artifact"],
    }
    baseline_source = {
        "generation_id": selected_payload["baseline"]["generation_id"],
        "artifact": selected_payload["baseline"]["artifact"],
    }
    classified, classified_snapshot, classified_artifact, policy_identity = (
        _c3_classified_flows(selected_inputs, ledger_pair, flows, anchors)
    )
    cutoff = _current_epoch_calculation_cutoff(selected_inputs)
    cutoff_excluded_opening_anchor_keys: set[tuple[object, ...]] = set()
    cutoff_excluded_allocation_keys: set[tuple[object, ...]] = set()
    cutoff_excluded_flow_keys: set[tuple[object, ...]] = set()
    if cutoff is not None:
        cutoff_excluded_opening_anchor_keys = {
            tuple(_c3_assignment_record(assignment).values())
            for assignment in mapped
            if any(
                anchor.get("capital_type") == "OPENING_CAPITAL_ANCHOR"
                and anchor.get("account_id") == assignment["account_id"]
                and anchor.get("strategy_id") == assignment["strategy_id"]
                and (
                    date.fromisoformat(str(anchor["event_date"])) > cutoff
                    or date.fromisoformat(
                        str(anchor.get("nav_recognition_date", anchor["event_date"]))
                    )
                    > cutoff
                )
                for anchor in anchors
            )
        }
        cutoff_excluded_allocation_keys = {
            tuple(_c3_assignment_record(assignment).values())
            for assignment in mapped
            if any(
                anchor.get("capital_type") == "STRATEGY_CAPITAL_ALLOCATION"
                and anchor.get("account_id") == assignment["account_id"]
                and anchor.get("strategy_id") == assignment["strategy_id"]
                and _c3_in_interval(str(anchor["event_date"]), assignment)
                and date.fromisoformat(str(anchor["event_date"])) <= cutoff
                and date.fromisoformat(
                    str(anchor.get("nav_recognition_date", anchor["event_date"]))
                )
                > cutoff
                for anchor in anchors
            )
        }
        cutoff_excluded_flow_keys = {
            tuple(_c3_assignment_record(assignment).values())
            for assignment in mapped
            if any(
                flow.get("account_id") == assignment["account_id"]
                and _c3_in_interval(str(flow["event_date"]), assignment)
                and date.fromisoformat(str(flow["event_date"])) <= cutoff
                and date.fromisoformat(
                    str(flow.get("nav_recognition_date", flow["event_date"]))
                )
                > cutoff
                for flow in flows
            )
        }
        navs = [
            item
            for item in navs
            if date.fromisoformat(str(item["as_of_date"])) <= cutoff
        ]
        flows = [
            item
            for item in flows
            if date.fromisoformat(str(item["event_date"])) <= cutoff
            and date.fromisoformat(
                str(item.get("nav_recognition_date", item["event_date"]))
            )
            <= cutoff
        ]
        anchors = [
            item
            for item in anchors
            if date.fromisoformat(str(item["event_date"])) <= cutoff
            and (
                item.get("capital_type") == "STRATEGY_CAPITAL_ALLOCATION"
                or date.fromisoformat(
                    str(item.get("nav_recognition_date", item["event_date"]))
                )
                <= cutoff
            )
        ]
        calculation_events = [
            item
            for item in classified["events"]
            if date.fromisoformat(str(item["event_date"])) <= cutoff
            and date.fromisoformat(
                str(item.get("nav_recognition_date", item["event_date"]))
            )
            <= cutoff
        ]
    else:
        calculation_events = classified["events"]
    routes = _mixed_receipt_routes(
        selected_inputs,
        mapped,
        frozenset() if sleeve is None else sleeve.residual_parents,
    )
    fixed_mixed_paths = {
        *(path.casefold() for path in _NINE_BLOCKED_PATHS),
        _CROSSWALK_SUBJECT_PATH.casefold(),
        "authority/account-interval-retrieval-receipt-authority.json".casefold(),
        "verification/independent-verification.json".casefold(),
    }
    if any(
        str(route["receipt_id"]).casefold() in fixed_mixed_paths for route in routes
    ):
        raise AccountAssignmentPerimeterError(
            "receipt artifact_id collides with a fixed mixed subject"
        )
    for route in routes:
        _validate_mixed_receipt_provider_evidence(
            selected_inputs,
            route["receipt"],
            providers[str(route["receipt"]["account_id"])],
        )
    coverage_payload = _mixed_coverage_payload(
        selected_inputs=selected_inputs,
        mapped=mapped,
        ledger_identity=ledger_pair,
        classified_artifact=classified_artifact,
        assignment_identity=assignment_identity,
        routes=routes,
        sleeve=sleeve,
    )
    coverage_snapshot = InputSnapshot.from_bytes(
        "mixed C3 coverage", canonical_json_bytes(coverage_payload)
    )
    try:
        load_flow_coverage(
            coverage_snapshot,
            ledger_pair,
            classified_artifact,
            selected_inputs.retrieval_receipts,
            mapped,
            selected_inputs.receipt_authority,
            () if sleeve is None else sleeve.coverage,
        )
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(
            "selected receipt coverage is invalid"
        ) from exc
    coverage_artifact = _c3_artifact(
        coverage_snapshot,
        "authority/account-flow-coverage.json",
        "account-flow-coverage",
        _selected_top_level_date(selected_inputs.provider_ledger).isoformat(),
        "2",
    )
    funded_assignments = _mixed_funded_assignments(selected_inputs, mapped, sleeve)
    calculation_routes = _sleeve_calculation_routes(sleeve, mapped, routes)
    projected_navs, projected_flows, projected_events, projected_anchors, projection = (
        _mixed_admitted_fact_projection(
            funded_assignments=funded_assignments,
            routes=calculation_routes,
            navs=navs,
            flows=flows,
            events=calculation_events,
            capital_anchors=anchors,
        )
    )
    currency = _mixed_projection_accounting_currency(
        projected_navs, projected_flows, projected_anchors
    )
    calculation_cutoff_date = (
        cutoff.isoformat()
        if cutoff is not None
        else selected_inputs.generation_as_of_date.isoformat()
    )
    calculation_ledger = {
        "accounting_currency": currency,
        "calculation_cutoff_date": calculation_cutoff_date,
        "nav_observations": projected_navs,
        "flows": projected_flows,
        "capital_anchors": projected_anchors,
    }
    selected_ledger = selected_inputs.provider_ledger.json_object(
        "selected provider ledger"
    )
    if "portfolio_fx_generation" in selected_ledger:
        calculation_ledger["portfolio_fx_generation"] = selected_ledger[
            "portfolio_fx_generation"
        ]
    calculation_ledger_snapshot = InputSnapshot.from_bytes(
        "calculation ledger", canonical_json_bytes(calculation_ledger)
    )
    calculation_ledger_artifact = _c3_artifact(
        calculation_ledger_snapshot,
        "authority/calculation-ledger.json",
        "account-manifest-calculation-ledger",
        selected_inputs.generation_as_of_date.isoformat(),
    )
    calculation_ledger_identity_payload = {
        "schema_version": 1,
        "generation_id": calculation_ledger_snapshot.sha256,
        "artifact": calculation_ledger_artifact,
    }
    calculation_ledger_identity_snapshot = InputSnapshot.from_bytes(
        "calculation ledger identity",
        canonical_json_bytes(calculation_ledger_identity_payload),
    )
    calculation_ledger_pair = {
        "generation_id": calculation_ledger_snapshot.sha256,
        "artifact": calculation_ledger_artifact,
    }
    policy_projection = canonical_json_bytes(
        selected_inputs.flow_classification_policy.json_object("selected flow policy")
    )
    policy_snapshot = InputSnapshot.from_bytes("policy projection", policy_projection)
    calculation_events: list[dict[str, object]] = []
    for event in projected_events:
        amount = event.get("amount")
        if not isinstance(amount, Mapping) or not isinstance(amount.get("value"), str):
            raise AccountAssignmentPerimeterError(
                "mixed calculation flow amount is invalid"
            )
        projected_event = {
            "event_id": event["event_id"],
            "source_record_id": event["source_record_id"],
            "account_id": event["account_id"],
            "event_date": event["event_date"],
            "nav_recognition_date": event["nav_recognition_date"],
            "amount": amount["value"],
            "disposition": event["disposition"],
        }
        if "counterparty_source_record_id" in event:
            projected_event["counterparty_source_record_id"] = event[
                "counterparty_source_record_id"
            ]
        calculation_events.append(projected_event)
    calculation_classified_payload = {
        "schema_version": 4,
        "artifact_id": "authority/calculation-classified-flows.json",
        "as_of_date": selected_inputs.generation_as_of_date.isoformat(),
        "ledger_identity": calculation_ledger_pair,
        "classification_policy": {
            "policy_id": policy_identity["policy_id"],
            "policy_version": policy_identity["policy_version"],
            "policy_as_of_date": selected_inputs.flow_classification_policy.json_object(
                "selected flow policy"
            )["as_of_date"],
            "policy_sha256": policy_snapshot.sha256,
        },
        "events": calculation_events,
        "capital_anchors": projected_anchors,
    }
    calculation_classified_snapshot = InputSnapshot.from_bytes(
        "calculation classified flows",
        canonical_json_bytes(calculation_classified_payload),
    )
    calculation_classified_artifact = _c3_artifact(
        calculation_classified_snapshot,
        "authority/calculation-classified-flows.json",
        "classified-flows",
        selected_inputs.generation_as_of_date.isoformat(),
        "4",
    )
    calculation_coverage_payload = _mixed_coverage_payload(
        selected_inputs=selected_inputs,
        mapped=mapped,
        ledger_identity=calculation_ledger_pair,
        classified_artifact=calculation_classified_artifact,
        assignment_identity=assignment_identity,
        routes=routes,
        sleeve=sleeve,
    )
    calculation_coverage_snapshot = InputSnapshot.from_bytes(
        "calculation coverage", canonical_json_bytes(calculation_coverage_payload)
    )
    calculation_payload = {
        "schema_version": 4,
        "artifact_id": "authority/calculation-inputs.json",
        "as_of_date": selected_inputs.generation_as_of_date.isoformat(),
        "accounting_currency": currency,
        "source_ledger_identity": ledger_source,
        "source_classified_flows_artifact": classified_artifact,
        "mapped_assignments_identity": assignment_identity,
        "projection": projection,
        "rows": [
            {
                "epoch": _c3_assignment_record(assignment),
                "provider": providers[str(assignment["account_id"])],
                "nav_observations": [
                    item
                    for item in projected_navs
                    if item["account_id"] == assignment["account_id"]
                    and _c3_in_interval(str(item["as_of_date"]), assignment)
                ],
                "classified_flows": [
                    item
                    for item in projected_events
                    if item["account_id"] == assignment["account_id"]
                    and _c3_in_interval(
                        str(item.get("nav_recognition_date", item["event_date"])),
                        assignment,
                    )
                ],
                "capital_anchors": [
                    item
                    for item in projected_anchors
                    if item["account_id"] == assignment["account_id"]
                    and item["strategy_id"] == assignment["strategy_id"]
                    and _c3_in_interval(
                        str(item.get("nav_recognition_date", item["event_date"])),
                        assignment,
                    )
                ],
            }
            for assignment in mapped
        ],
        "calculation_ledger": calculation_ledger,
        "calculation_ledger_identity": calculation_ledger_identity_payload,
        "calculation_classified_flows": calculation_classified_payload,
        "calculation_flow_coverage": calculation_coverage_payload,
        "derivation": "FUNDED_EPOCH_FACT_PROJECTION_V2",
    }
    calculation_snapshot = InputSnapshot.from_bytes(
        "mixed calculation inputs", canonical_json_bytes(calculation_payload)
    )
    calculation_artifact = _c3_artifact(
        calculation_snapshot,
        "authority/calculation-inputs.json",
        "account-manifest-calculation-inputs",
        selected_inputs.generation_as_of_date.isoformat(),
        "4",
    )
    admission_payload = _effective_admission_payload(selected_inputs, sleeve)
    selected_keys = {
        tuple(_c3_assignment_record(assignment).values()) for assignment in mapped
    }
    admission_payload["admissions"] = [
        row
        for row in admission_payload["admissions"]
        if tuple(
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
    admission_payload["assignment_sha256"] = hashlib.sha256(
        json.dumps(
            sorted(
                [_c3_assignment_record(assignment) for assignment in mapped],
                key=lambda item: (
                    item["account_id"],
                    item["strategy_id"],
                    item["effective_start"],
                    str(item["effective_end"]),
                ),
            ),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    admission_snapshot = InputSnapshot.from_bytes(
        "selected admission policy", canonical_json_bytes(admission_payload)
    )
    candidate = build_manifest_candidate_from_snapshots(
        ManifestCandidateSnapshots(
            ledger=calculation_ledger_snapshot,
            ledger_identity=calculation_ledger_identity_snapshot,
            baseline=selected_inputs.baseline,
            classified_flows=calculation_classified_snapshot,
            classification_policy=policy_snapshot,
            admission_policy=admission_snapshot,
            flow_coverage=calculation_coverage_snapshot,
            retrieval_receipts=selected_inputs.retrieval_receipts,
            receipt_authority=selected_inputs.receipt_authority,
            calculation_code=selected_inputs.epoch_producer_source,
            manifest_schema=selected_inputs.manifest_schema,
            baseline_generation_id=selected_inputs.baseline_generation_id,
            membership_census=_require_snapshot(
                selected_inputs.membership_census, "membership_census"
            ),
            selected_assignment_keys=tuple(
                tuple(_c3_assignment_record(assignment).values())
                for assignment in mapped
            ),
            supporting_internal_counterparty_source_record_ids=tuple(
                projection["supporting_internal_counterparty_source_record_ids"]
            ),
            sleeve_facts=selected_inputs.sleeve_facts,
        )
    )
    candidate_rows = candidate.get("rows")
    if not isinstance(candidate_rows, list):
        raise AccountAssignmentPerimeterError("generic candidate rows are invalid")
    numeric_projection_fields = {
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
        "observed_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observation_trace",
        "nav_observations",
        *PERFORMANCE_ROW_FIELDS,
        *PERIOD_RETURN_FIELDS,
    }
    cutoff_excluded_keys = (
        cutoff_excluded_opening_anchor_keys
        | cutoff_excluded_allocation_keys
        | cutoff_excluded_flow_keys
    )
    candidate["rows"] = [
        {
            **{
                field: value
                for field, value in row.items()
                if field not in numeric_projection_fields
            },
            "status": "BLOCKED_RETURN_LINKAGE",
            "blocking_reason": (
                blocking_reason := (
                    "OPENING_ANCHOR_RECOGNITION_AFTER_CUTOFF"
                    if key in cutoff_excluded_opening_anchor_keys
                    else "FLOW_RECOGNITION_NOT_LINKED_TO_OBSERVED_INTERVAL"
                )
            ),
            "mwr_status": "BLOCKED_ADMISSION",
            "mwr_reason": blocking_reason,
            "mwr_xirr": None,
            "mwr_opening_date": None,
            "mwr_opening_value": None,
            "mwr_terminal_date": None,
            "mwr_terminal_value": None,
            "mwr_currency": None,
            "mwr_coverage_status": "BLOCKED",
            "mwr_cashflows": [],
            "mwr_label": None,
            "mwr_value_basis": None,
            "mwr_valuation_timing": None,
            "mwr_day_count": "ACTUAL_365",
            "mwr_unit": "ANNUALIZED_RETURN",
            "mwr_source_binding": "GOVERNED_SOURCE_IDENTITIES",
        }
        if isinstance(row, dict)
        and (
            key := tuple(
                row.get(field)
                for field in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            )
        )
        in cutoff_excluded_keys
        and row.get("status") == "OK"
        else row
        for row in candidate_rows
    ]
    source_identities = candidate.get("source_identities")
    if not isinstance(source_identities, Mapping):
        raise AccountAssignmentPerimeterError(
            "generic candidate source identities are invalid"
        )
    candidate_ledger = source_identities.get("ledger")
    candidate_classified = source_identities.get("classified_flows")
    candidate_coverage = source_identities.get("flow_coverage")
    candidate_admission = source_identities.get("admission_policy")
    if not all(
        isinstance(value, Mapping)
        for value in (
            candidate_ledger,
            candidate_classified,
            candidate_coverage,
            candidate_admission,
        )
    ):
        raise AccountAssignmentPerimeterError(
            "generic candidate source identities are invalid"
        )
    expected_calculation_ledger = {
        **calculation_ledger_pair,
        "identity_artifact": _c3_artifact(
            calculation_ledger_identity_snapshot,
            "identity/ledger-source.json",
            "ledger-source-identity",
            selected_inputs.generation_as_of_date.isoformat(),
        ),
    }
    expected_classified = {
        "artifact": calculation_classified_artifact,
        "schema_version": 4,
        "ledger_identity": expected_calculation_ledger,
        "classification_policy": calculation_classified_payload[
            "classification_policy"
        ],
    }
    expected_authority_artifact = _c3_artifact(
        selected_inputs.receipt_authority,
        "authority/account-interval-retrieval-receipt-authority.json",
        "account-interval-retrieval-receipt-authority",
        str(
            selected_inputs.receipt_authority.json_object(
                "selected receipt authority"
            ).get("as_of_date")
        ),
    )
    expected_calculation_coverage_artifact = _c3_artifact(
        calculation_coverage_snapshot,
        "authority/account-flow-coverage.json",
        "account-flow-coverage",
        _selected_top_level_date(selected_inputs.provider_ledger).isoformat(),
        "2",
    )
    expected_coverage = {
        "artifact": expected_calculation_coverage_artifact,
        "ledger_identity": expected_calculation_ledger,
        "classified_flows_artifact": calculation_classified_artifact,
        "receipt_authority": expected_authority_artifact,
    }
    selected_baseline_payload = selected_inputs.baseline.json_object(
        "selected baseline"
    )
    expected_candidate_baseline = {
        "generation_id": selected_inputs.baseline_generation_id,
        "artifact": _c3_artifact(
            selected_inputs.baseline,
            "normalized/portfolio-baseline.json",
            "portfolio-baseline",
            str(selected_baseline_payload.get("as_of_date")),
            str(selected_baseline_payload.get("baseline_schema_version")),
        ),
    }
    if (
        candidate_ledger != expected_calculation_ledger
        or candidate_classified != expected_classified
        or candidate_coverage != expected_coverage
        or source_identities.get("baseline") != expected_candidate_baseline
        or source_identities.get("assignment") != assignment_identity
        or source_identities.get("calculation_code")
        != selected_payload["code_sources"]["epoch_producer"]
    ):
        raise AccountAssignmentPerimeterError(
            "generic candidate calculation identities differ"
        )
    expected_admission_artifact = _c3_artifact(
        admission_snapshot,
        str(admission_payload.get("artifact_id")),
        "account-strategy-manifest-admission-policy",
        str(admission_payload.get("as_of_date")),
        "2",
    )
    if candidate_admission != {
        "policy_id": admission_payload.get("policy_id"),
        "policy_version": admission_payload.get("policy_version"),
        "artifact": expected_admission_artifact,
        "assignment": assignment_identity,
    }:
        raise AccountAssignmentPerimeterError(
            "generic candidate admission identity differs"
        )
    if candidate_coverage.get("receipt_authority") != expected_authority_artifact:
        raise AccountAssignmentPerimeterError(
            "generic candidate receipt authority identity differs"
        )
    rows = candidate.get("rows")
    funded_keys = {
        tuple(_c3_assignment_record(assignment).values())
        for assignment in funded_assignments
    }
    expected_calculated_keys = {
        tuple(_c3_assignment_record(route["assignment"]).values())
        for route in calculation_routes
        if tuple(_c3_assignment_record(route["assignment"]).values()) in funded_keys
    }
    actual_calculated_keys = (
        {
            tuple(
                row.get(key)
                for key in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            )
            for row in rows
            if isinstance(row, Mapping)
            and row.get("status") in {"OK", "BLOCKED_RETURN_LINKAGE"}
        }
        if isinstance(rows, list)
        else set()
    )
    if not isinstance(rows, list) or actual_calculated_keys != expected_calculated_keys:
        raise AccountAssignmentPerimeterError(
            "mixed composition calculated rows differ from funded receipt routes"
        )
    if selected_inputs.current_epoch_crosswalk is not None:
        display_by_epoch = _crosswalk_display_by_epoch(
            selected_inputs.current_epoch_crosswalk
        )
        disposition_by_epoch = _crosswalk_dispositions_by_epoch(
            selected_inputs.current_epoch_crosswalk
        )
        if sleeve is not None:
            display_by_epoch.update(_sleeve_display_by_epoch(sleeve, display_by_epoch))
            # a derived sleeve epoch is a funded product epoch of its own
            for key in _sleeve_display_by_epoch(sleeve, display_by_epoch):
                disposition_by_epoch[key] = "INCLUDED_PRODUCT_EPOCH"
        epoch_keys = [
            tuple(
                row.get(field)
                for field in (
                    "account_id",
                    "strategy_id",
                    "effective_start",
                    "effective_end",
                )
            )
            for row in rows
            if isinstance(row, Mapping)
        ]
        rows = [
            {
                **dict(row),
                **display_by_epoch[key],
                **_epoch_lifecycle_fields(row, disposition_by_epoch[key]),
            }
            for row, key in zip(
                (row for row in rows if isinstance(row, Mapping)), epoch_keys
            )
        ]
    if len(rows) != len(mapped):
        raise AccountAssignmentPerimeterError("mixed output rows are invalid")
    forbidden = {
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
        "observed_return",
        "observed_hwm_index",
        "observed_max_drawdown",
        "opening_basis",
        "observation_trace",
        "capital_flow_trace",
        "nav_observation_trace",
        "nav_observations",
        *PERFORMANCE_ROW_FIELDS,
        *PERIOD_RETURN_FIELDS,
    }
    if any(
        forbidden & set(row)
        for row in rows
        if isinstance(row, Mapping) and row.get("status") != "OK"
    ):
        raise AccountAssignmentPerimeterError(
            "mixed composition blocked rows are not numeric-free"
        )
    nav_facts = [
        {
            "record_id": item["record_id"],
            "account_id": item["account_id"],
            "as_of_date": item["as_of_date"],
            "nav": {"status": item["nav"]["status"]},
        }
        for item in lineage_navs
    ]
    raw_facts = [
        {
            "record_id": item["record_id"],
            "account_id": item["account_id"],
            "event_date": item["event_date"],
        }
        for item in lineage_flows
    ]
    classified_facts = [
        {
            "event_id": item["event_id"],
            "source_record_id": item["source_record_id"],
            "account_id": item["account_id"],
            "event_date": item["event_date"],
        }
        for item in classified["events"]
    ]
    perimeter_bytes = build_account_assignment_perimeter(
        selected_inputs=selected_inputs,
        nav_facts=nav_facts,
        raw_flow_facts=raw_facts,
        classified_flow_facts=classified_facts,
    )
    perimeter_snapshot = InputSnapshot.from_bytes("mixed perimeter", perimeter_bytes)
    policy_artifact = _c3_artifact(
        policy_snapshot,
        "authority/flow-classification-policy.json",
        "flow-classification-policy",
        str(
            selected_inputs.flow_classification_policy.json_object(
                "selected flow policy"
            )["as_of_date"]
        ),
    )
    code_snapshot = InputSnapshot.from_bytes(
        "code identities",
        canonical_json_bytes(
            {
                "schema_version": 1,
                "artifact_id": "authority/code-identities.json",
                "as_of_date": selected_inputs.generation_as_of_date.isoformat(),
                "code_sources": selected_payload["code_sources"],
            }
        ),
    )
    output = {
        "schema_version": 10,
        "lifecycle_schema_version": _LIFECYCLE_SCHEMA_VERSION,
        "artifact": "account-strategy-manifest",
        "accounting_currency": currency,
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
                "schema_version": 5,
                "ledger_identity": ledger_source,
                "classification_policy": {
                    "policy_id": policy_identity["policy_id"],
                    "policy_version": policy_identity["policy_version"],
                    "policy_as_of_date": selected_inputs.flow_classification_policy.json_object(
                        "selected flow policy"
                    )["as_of_date"],
                    "policy_sha256": policy_snapshot.sha256,
                },
            },
            "flow_classification_policy": {
                **policy_identity,
                "artifact": policy_artifact,
            },
            "flow_coverage": {
                "artifact": coverage_artifact,
                "ledger_identity": ledger_source,
                "classified_flows_artifact": classified_artifact,
                "receipt_authority": candidate_coverage["receipt_authority"],
            },
            "admission_policy": candidate_admission,
            "assignment": assignment_identity,
            "calculation_code": selected_payload["code_sources"]["epoch_producer"],
            "selected_inputs": _c3_artifact(
                InputSnapshot.from_bytes(
                    "selected identities", canonical_json_bytes(selected_payload)
                ),
                "authority/selected-input-identities.json",
                "selected-input-identities",
                selected_inputs.generation_as_of_date.isoformat(),
            ),
            "account_assignment_perimeter": _c3_artifact(
                perimeter_snapshot,
                "authority/account-assignment-perimeter.json",
                "account-assignment-perimeter",
                selected_inputs.generation_as_of_date.isoformat(),
            ),
            "calculation_inputs": calculation_artifact,
            "code_identities": _c3_artifact(
                code_snapshot,
                "authority/code-identities.json",
                "account-manifest-code-identities",
                selected_inputs.generation_as_of_date.isoformat(),
            ),
        },
        "epoch_continuity": candidate["epoch_continuity"],
        "flow_reconciliation": candidate["flow_reconciliation"],
        "rows": rows,
    }
    output["calculation_cutoff_date"] = calculation_cutoff_date
    try:
        validate_manifest_schema(
            output, schema_snapshot=selected_inputs.manifest_schema
        )
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError(
            "mixed final manifest is invalid"
        ) from exc
    mixed_payloads = {
        "authority/account-assignment-perimeter.json": perimeter_bytes,
        "authority/account-flow-coverage.json": coverage_snapshot.contents,
        "authority/calculation-inputs.json": calculation_snapshot.contents,
        "authority/classified-flows.json": classified_snapshot.contents,
        "authority/code-identities.json": code_snapshot.contents,
        "authority/flow-classification-policy.json": policy_projection,
        "authority/mapped-epoch-admission.json": admission_snapshot.contents,
        "authority/selected-input-identities.json": canonical_json_bytes(
            selected_payload
        ),
        "outputs/account-strategy-manifest.json": canonical_json_bytes(output),
        "authority/account-interval-retrieval-receipt-authority.json": (
            selected_inputs.receipt_authority.contents
        ),
    }
    if selected_inputs.current_epoch_crosswalk is not None:
        mixed_payloads[_CROSSWALK_SUBJECT_PATH] = (
            selected_inputs.current_epoch_crosswalk.contents
        )
    mixed_payloads.update(
        {
            receipt_id: selected_inputs.retrieval_receipts[receipt_id].contents
            for receipt_id in sorted(
                selected_inputs.retrieval_receipts, key=str.casefold
            )
        }
    )
    return MappingProxyType(mixed_payloads)


def _load_attested_independent_verifier(verifier_path: Path) -> object:
    """Execute the attested verifier bytes in a fresh, uncached module object."""

    module_name = "_quant_engine_attested_account_manifest_verifier"
    spec = importlib.util.spec_from_file_location(module_name, verifier_path)
    if spec is None or spec.loader is None:
        raise AccountAssignmentPerimeterError(
            "independent verifier source cannot be loaded"
        )
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise AccountAssignmentPerimeterError(
            "independent verifier source cannot be loaded"
        ) from exc
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


def _generation_artifact_paths(
    selected_inputs: AccountManifestSelectedInputs,
) -> tuple[str, ...]:
    fixed = (
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
    if selected_inputs.receipt_authority is None:
        return fixed
    if selected_inputs.current_epoch_crosswalk is None:
        raise AccountAssignmentPerimeterError(
            "mixed current-product generation requires current_epoch_crosswalk"
        )
    return (
        *fixed,
        *(
            ()
            if selected_inputs.current_epoch_crosswalk is None
            else (_CROSSWALK_SUBJECT_PATH,)
        ),
        "authority/account-interval-retrieval-receipt-authority.json",
        *sorted(selected_inputs.retrieval_receipts, key=str.casefold),
    )


def _payload_artifact_date(
    *,
    artifact_id: str,
    payload: bytes,
    selected_inputs: AccountManifestSelectedInputs,
) -> date:
    if artifact_id in selected_inputs.retrieval_receipts:
        receipt = selected_inputs.retrieval_receipts[artifact_id].json_object(
            "selected retrieval receipt"
        )
        if receipt.get("coverage_mode") == "STATIC_SNAPSHOT_COVERAGE_V1":
            return date.fromisoformat(
                _iso_date(receipt.get("requested_end"), "static receipt requested_end")
            )
        return date.fromisoformat(
            _iso_date(receipt.get("covered_end"), "receipt covered_end")
        )
    if artifact_id == "outputs/account-strategy-manifest.json":
        return selected_inputs.generation_as_of_date
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AccountAssignmentPerimeterError(
            "generated artifact must contain JSON"
        ) from None
    if not isinstance(decoded, Mapping):
        raise AccountAssignmentPerimeterError(
            "generated artifact must contain a JSON object"
        )
    return date.fromisoformat(
        _iso_date(decoded.get("as_of_date"), "generated artifact as_of_date")
    )


def _artifact_schema(
    artifact_id: str,
    *,
    selected_inputs: AccountManifestSelectedInputs,
    mixed: bool,
    report_schema_version: int | None = None,
) -> tuple[str, str]:
    if artifact_id == _INDEPENDENT_REPORT_PATH:
        if report_schema_version not in {2, 3}:
            raise AccountAssignmentPerimeterError(
                "independent verification report schema version is invalid"
            )
        return "account-manifest-independent-verification", str(report_schema_version)
    if artifact_id in _SUBJECT_SCHEMAS:
        if not mixed and artifact_id == "authority/calculation-inputs.json":
            return "account-manifest-calculation-inputs", "1"
        if not mixed and artifact_id == "authority/classified-flows.json":
            return "classified-flows", "4"
        if not mixed and artifact_id == "outputs/account-strategy-manifest.json":
            return "account-strategy-manifest", "8"
        if not mixed and artifact_id == "authority/mapped-epoch-admission.json":
            return "mapped-epoch-admission", "1"
        return _SUBJECT_SCHEMAS[artifact_id]
    if artifact_id in selected_inputs.retrieval_receipts:
        receipt = selected_inputs.retrieval_receipts[artifact_id].json_object(
            "selected retrieval receipt"
        )
        return "account-interval-retrieval-receipt", (
            "3"
            if receipt.get("coverage_mode") == "STATIC_SNAPSHOT_COVERAGE_V1"
            else "2"
        )
    raise AccountAssignmentPerimeterError(
        "generated artifact has no approved schema mapping"
    )


def _source_artifact(
    *,
    artifact_id: str,
    payload: bytes,
    selected_inputs: AccountManifestSelectedInputs,
    mixed: bool,
    report_schema_version: int | None = None,
) -> SourceArtifact:
    schema_name, schema_version = _artifact_schema(
        artifact_id,
        selected_inputs=selected_inputs,
        mixed=mixed,
        report_schema_version=report_schema_version,
    )
    return SourceArtifact(
        artifact_id=artifact_id,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        as_of_date=_payload_artifact_date(
            artifact_id=artifact_id,
            payload=payload,
            selected_inputs=selected_inputs,
        ),
        schema_name=schema_name,
        schema_version=schema_version,
    )


def stage_account_manifest_generation(
    selected_inputs: AccountManifestSelectedInputs,
    generation_root: Path,
) -> GenerationBundle:
    """Verify, durably stage, and read back one exact account-manifest bundle."""

    if type(selected_inputs) is not AccountManifestSelectedInputs:
        raise AccountAssignmentPerimeterError(
            "selected_inputs must be an AccountManifestSelectedInputs"
        )
    if (
        selected_inputs.receipt_authority is not None
        and selected_inputs.current_epoch_crosswalk is None
    ):
        raise AccountAssignmentPerimeterError(
            "mixed current-product generation requires current_epoch_crosswalk"
        )

    builder_path = Path(__file__).resolve()
    epoch_producer_path = builder_path.with_name("account_strategy_manifest.py")
    verifier_path = builder_path.with_name("verify_account_manifest_generation.py")
    try:
        epoch_producer_bytes = epoch_producer_path.read_bytes()
        builder_bytes = builder_path.read_bytes()
        verifier_bytes = verifier_path.read_bytes()
    except OSError as exc:
        raise AccountAssignmentPerimeterError(
            "selected generation code cannot be attested"
        ) from exc
    if (
        epoch_producer_bytes != selected_inputs.epoch_producer_source.contents
        or builder_bytes != selected_inputs.generation_builder_source.contents
        or verifier_bytes != selected_inputs.independent_verifier_source.contents
    ):
        raise AccountAssignmentPerimeterError(
            "selected generation code differs from executing bytes"
        )

    subjects = dict(build_account_manifest_payloads(selected_inputs))
    expected_subject_paths = _generation_artifact_paths(selected_inputs)
    if set(subjects) != set(expected_subject_paths):
        raise AccountAssignmentPerimeterError("generated subject path set is invalid")

    verifier_module = _load_attested_independent_verifier(verifier_path)
    verifier_inputs_type = getattr(verifier_module, "IndependentVerifierInputs", None)
    verifier_type = getattr(verifier_module, "IndependentAccountManifestVerifier", None)
    if not isinstance(verifier_inputs_type, type) or not isinstance(
        verifier_type, type
    ):
        raise AccountAssignmentPerimeterError(
            "independent verifier source has no approved public API"
        )
    verifier_inputs = verifier_inputs_type(
        provider_ledger=selected_inputs.provider_ledger,
        provider_ledger_manifest=selected_inputs.provider_ledger_manifest,
        baseline=selected_inputs.baseline,
        baseline_manifest=selected_inputs.baseline_manifest,
        flow_classification_policy=selected_inputs.flow_classification_policy,
        retrieval_receipts=selected_inputs.retrieval_receipts,
        epoch_producer_source=selected_inputs.epoch_producer_source,
        generation_builder_source=selected_inputs.generation_builder_source,
        independent_verifier_source=selected_inputs.independent_verifier_source,
        receipt_authority=selected_inputs.receipt_authority,
        admission_policy=selected_inputs.admission_policy,
        manifest_schema=selected_inputs.manifest_schema,
        membership_census=(
            None
            if selected_inputs.membership_census is None
            else _require_snapshot(
                selected_inputs.membership_census, "membership_census"
            )
        ),
        current_epoch_crosswalk=selected_inputs.current_epoch_crosswalk,
        **(
            {"sleeve_facts": selected_inputs.sleeve_facts}
            if selected_inputs.sleeve_facts
            else {}
        ),
    )
    report = verifier_type().verify(verifier_inputs, subjects)
    if type(report) is not bytes:
        raise AccountAssignmentPerimeterError(
            "independent verifier did not return exact report bytes"
        )
    try:
        report_payload = json.loads(report)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AccountAssignmentPerimeterError(
            "independent verifier returned invalid report bytes"
        ) from None
    if not isinstance(report_payload, Mapping):
        raise AccountAssignmentPerimeterError(
            "independent verifier returned an invalid report"
        )
    report_schema_version = report_payload.get("schema_version")
    if isinstance(report_schema_version, bool) or not isinstance(
        report_schema_version, int
    ):
        raise AccountAssignmentPerimeterError(
            "independent verification report schema version is invalid"
        )
    payloads = {**subjects, _INDEPENDENT_REPORT_PATH: report}
    mixed = selected_inputs.receipt_authority is not None
    artifact_paths = (*expected_subject_paths, _INDEPENDENT_REPORT_PATH)
    artifacts = tuple(
        _source_artifact(
            artifact_id=artifact_id,
            payload=payloads[artifact_id],
            selected_inputs=selected_inputs,
            mixed=mixed,
            report_schema_version=(
                report_schema_version
                if artifact_id == _INDEPENDENT_REPORT_PATH
                else None
            ),
        )
        for artifact_id in artifact_paths
    )
    manifest = GenerationManifest.build(
        # Retained authority can be newer than the financial source snapshots.
        # Date the bundle by its latest artifact without advancing those
        # snapshots, calculation cutoff, or coverage observations.
        as_of_date=max(artifact.as_of_date for artifact in artifacts),
        adapter=_GENERATION_ADAPTER,
        coverage=CoverageClaim(
            status=CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY,
            boundary=_GENERATION_COVERAGE_BOUNDARY,
            fact_types=(
                "account-manifest-subject-bytes",
                "independent-verification-report-bytes",
            ),
            observed_start=selected_inputs.generation_as_of_date,
            observed_end=selected_inputs.generation_as_of_date,
        ),
        artifacts=artifacts,
        verification=VerificationStatus.VERIFIED,
    )
    if set(payloads) != {artifact.artifact_id for artifact in manifest.artifacts}:
        raise AccountAssignmentPerimeterError(
            "generation payload keys differ from manifest artifacts"
        )
    stage_generation_bundle(generation_root, manifest, payloads)
    return read_generation_bundle(generation_root, manifest.generation_id)
