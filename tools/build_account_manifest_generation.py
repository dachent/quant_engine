"""Build the pure, hash-bound account-assignment perimeter artifact."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence

from quant_engine.core import GenerationFileIdentity, InputSnapshot
from quant_engine.core.contracts import (
    GenerationManifest,
    ManifestValidationError,
    VerificationStatus,
    canonical_json_bytes,
)
from tools.account_strategy_manifest import (
    ManifestError,
    json_ready,
    load_flow_classification_policy,
    load_flow_coverage,
    partition_assignments,
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
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_INTERNAL_ACCOUNT_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_CANONICAL_CENT = re.compile(r"^-?(0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$")
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


class AccountAssignmentPerimeterError(ValueError):
    """The input boundary or proposed perimeter payload is invalid."""


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
    return max(_selected_top_level_date(ledger), _selected_top_level_date(selected_baseline))


def _nonnegative_schema_version(
    snapshot: InputSnapshot, field_name: str
) -> str:
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
        raise AccountAssignmentPerimeterError("selected manifest must use canonical bytes")
    if manifest.verification is not VerificationStatus.VERIFIED:
        raise AccountAssignmentPerimeterError("selected manifest must be VERIFIED")
    if _SHA256.fullmatch(manifest.generation_id) is None:
        raise AccountAssignmentPerimeterError("selected manifest generation_id is invalid")
    if manifest.as_of_date != source_date:
        raise AccountAssignmentPerimeterError(
            "selected manifest and catalog as_of_date differ"
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
            as_of_date=source_date,
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
    provider_ledger_generation_id: str = field(init=False)
    baseline_generation_id: str = field(init=False)
    generation_as_of_date: date = field(init=False)

    def __post_init__(self) -> None:
        provider_ledger = _require_snapshot(self.provider_ledger, "provider_ledger")
        provider_ledger_manifest = _require_snapshot(
            self.provider_ledger_manifest, "provider_ledger_manifest"
        )
        baseline = _require_snapshot(self.baseline, "baseline")
        baseline_manifest = _require_snapshot(self.baseline_manifest, "baseline_manifest")
        flow_policy = _require_snapshot(
            self.flow_classification_policy, "flow_classification_policy"
        )
        epoch_source = _require_snapshot(self.epoch_producer_source, "epoch_producer_source")
        builder_source = _require_snapshot(
            self.generation_builder_source, "generation_builder_source"
        )
        verifier_source = _require_snapshot(
            self.independent_verifier_source, "independent_verifier_source"
        )
        if not isinstance(self.retrieval_receipts, Mapping):
            raise AccountAssignmentPerimeterError("retrieval_receipts must be a mapping")
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
        object.__setattr__(self, "provider_ledger_generation_id", ledger_id)
        object.__setattr__(self, "baseline_generation_id", baseline_id)
        object.__setattr__(
            self,
            "generation_as_of_date",
            derive_generation_as_of_date(provider_ledger, baseline),
        )


def _selected_input_identities(selected_inputs: AccountManifestSelectedInputs) -> dict[str, object]:
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
    return {
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
            for receipt_id, receipt in sorted(selected_inputs.retrieval_receipts.items())
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
        raise AccountAssignmentPerimeterError("payload differs from expected selected inputs")


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
        raise AccountAssignmentPerimeterError(f"{field_name} must be a non-empty string")
    return value


def _iso_date(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise AccountAssignmentPerimeterError(f"{field_name} must be a canonical ISO date")
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
) -> tuple[dict[str, object], dict[str, str], list[dict[str, object]]]:
    if not isinstance(baseline, InputSnapshot):
        raise AccountAssignmentPerimeterError("baseline must be an InputSnapshot")
    try:
        payload = baseline.json_object("portfolio baseline")
    except ValueError as exc:
        raise AccountAssignmentPerimeterError("portfolio baseline is not strict JSON") from exc
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
    assignments = payload.get("assignments")
    if not isinstance(accounts, list) or not isinstance(assignments, list):
        raise AccountAssignmentPerimeterError(
            "baseline accounts and assignments must be lists"
        )
    if not accounts or not assignments:
        raise AccountAssignmentPerimeterError("baseline accounts and assignments must not be empty")
    providers: dict[str, str] = {}
    for index, account in enumerate(accounts):
        if not isinstance(account, Mapping):
            raise AccountAssignmentPerimeterError(f"baseline account {index} must be an object")
        account_id = _nonempty_text(account.get("account_id"), f"account[{index}].account_id")
        provider = _nonempty_text(account.get("provider"), f"account[{index}].provider")
        if account_id in providers:
            raise AccountAssignmentPerimeterError(f"duplicate baseline account_id '{account_id}'")
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
    if set(perimeter.full_chains) != set(providers):
        raise AccountAssignmentPerimeterError(
            "baseline account references differ from assignment references"
        )
    return payload, providers, normalized_assignments


def _assignment_preimage(assignment: Mapping[str, object]) -> dict[str, object]:
    return {
        "account_id": assignment["account_id"],
        "strategy_id": assignment["strategy_id"],
        "strategy_mapping_disposition": assignment["strategy_mapping_disposition"],
        "effective_start": assignment["effective_start"],
        "effective_end": assignment["effective_end"],
    }


def _row_sort_key(row: Mapping[str, object]) -> tuple[str, str, bool, str, bool, str, str]:
    effective_end = row["effective_end"]
    strategy_id = row["strategy_id"]
    assert isinstance(effective_end, str) or effective_end is None
    assert isinstance(strategy_id, str) or strategy_id is None
    return (
        str(row["account_id"]),
        str(row["effective_start"]),
        effective_end is None,
        effective_end or "",
        strategy_id is None,
        strategy_id or "",
        str(row["strategy_mapping_disposition"]),
    )


def _in_interval(fact_date: str, assignment: Mapping[str, object]) -> bool:
    effective_start = assignment["effective_start"]
    effective_end = assignment["effective_end"]
    assert isinstance(effective_start, str)
    assert isinstance(effective_end, str) or effective_end is None
    return fact_date >= effective_start and (
        effective_end is None or fact_date <= effective_end
    )


def _index_rows(
    assignments: list[dict[str, object]], providers: Mapping[str, str]
) -> tuple[list[dict[str, object]], dict[tuple[str, str, str | None, str | None], dict[str, object]]]:
    rows: list[dict[str, object]] = []
    by_interval: dict[tuple[str, str, str | None, str | None], dict[str, object]] = {}
    identities: set[str] = set()
    for assignment in assignments:
        preimage = _assignment_preimage(assignment)
        account_id = preimage["account_id"]
        effective_start = preimage["effective_start"]
        effective_end = preimage["effective_end"]
        strategy_id = preimage["strategy_id"]
        disposition = preimage["strategy_mapping_disposition"]
        assert isinstance(account_id, str)
        assert isinstance(effective_start, str)
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
    rows_by_interval: Mapping[tuple[str, str, str | None, str | None], dict[str, object]],
    known_accounts: Mapping[str, str],
) -> dict[str, object]:
    if account_id not in known_accounts:
        raise AccountAssignmentPerimeterError("unknown fact account_id")
    matches = [
        assignment for assignment in assignments if _in_interval(fact_date, assignment)
        and assignment["account_id"] == account_id
    ]
    if len(matches) != 1:
        raise AccountAssignmentPerimeterError(
            "fact is outside every assignment interval"
        )
    match = matches[0]
    key = (
        account_id,
        match["effective_start"],
        match["effective_end"],
        match["strategy_id"],
    )
    assert isinstance(key[1], str)
    assert isinstance(key[2], str) or key[2] is None
    assert isinstance(key[3], str) or key[3] is None
    return rows_by_interval[key]


def _count_facts(
    *,
    nav_facts: Sequence[Mapping[str, object]],
    raw_flow_facts: Sequence[Mapping[str, object]],
    classified_flow_facts: Sequence[Mapping[str, object]],
    assignments: Sequence[Mapping[str, object]],
    rows_by_interval: Mapping[tuple[str, str, str | None, str | None], dict[str, object]],
    known_accounts: Mapping[str, str],
) -> None:
    nav_record_ids: set[str] = set()
    nav_account_dates: set[tuple[str, str]] = set()
    for index, fact in enumerate(nav_facts):
        if not isinstance(fact, Mapping):
            raise AccountAssignmentPerimeterError(f"NAV fact {index} must be an object")
        record_id = _nonempty_text(fact.get("record_id"), f"NAV fact {index}.record_id")
        if record_id in nav_record_ids:
            raise AccountAssignmentPerimeterError("duplicate NAV record_id")
        nav_record_ids.add(record_id)
        account_id = _nonempty_text(fact.get("account_id"), f"NAV fact {index}.account_id")
        as_of_date = _iso_date(fact.get("as_of_date"), f"NAV fact {index}.as_of_date")
        account_date = (account_id, as_of_date)
        if account_date in nav_account_dates:
            raise AccountAssignmentPerimeterError("duplicate NAV account_id/as_of_date")
        nav_account_dates.add(account_date)
        nav = fact.get("nav")
        if not isinstance(nav, Mapping):
            raise AccountAssignmentPerimeterError(f"NAV fact {index}.nav must be an object")
        status = nav.get("status")
        if status not in {"AVAILABLE", "UNAVAILABLE"}:
            raise AccountAssignmentPerimeterError("NAV status must be AVAILABLE or UNAVAILABLE")
        row = _assignment_row_for_fact(
            account_id=account_id,
            fact_date=as_of_date,
            assignments=assignments,
            rows_by_interval=rows_by_interval,
            known_accounts=known_accounts,
        )
        row["available_nav" if status == "AVAILABLE" else "unavailable_nav"] += 1

    raw_record_ids: set[str] = set()
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
        row = _assignment_row_for_fact(
            account_id=account_id,
            fact_date=event_date,
            assignments=assignments,
            rows_by_interval=rows_by_interval,
            known_accounts=known_accounts,
        )
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
        account_id = _nonempty_text(
            fact.get("account_id"), f"classified flow fact {index}.account_id"
        )
        event_date = _iso_date(
            fact.get("event_date"), f"classified flow fact {index}.event_date"
        )
        row = _assignment_row_for_fact(
            account_id=account_id,
            fact_date=event_date,
            assignments=assignments,
            rows_by_interval=rows_by_interval,
            known_accounts=known_accounts,
        )
        row["classified_flows"] += 1


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
    baseline_payload, providers, assignments = _baseline_payload(selected_inputs.baseline)
    rows, rows_by_interval = _index_rows(assignments, providers)
    _count_facts(
        nav_facts=nav_facts,
        raw_flow_facts=raw_flow_facts,
        classified_flow_facts=classified_flow_facts,
        assignments=assignments,
        rows_by_interval=rows_by_interval,
        known_accounts=providers,
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
    partition_counts = Counter(
        str(row["strategy_mapping_disposition"]) for row in rows
    )
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
        "account_count": len(providers),
        "assignment_count": len(rows),
        "partition_counts": {
            disposition: partition_counts[disposition] for disposition in _DISPOSITIONS
        },
        "fact_bijection_status": "ONE_TO_ONE_VERIFIED",
        "unassigned_fact_count": 0,
        "unassigned_facts": [],
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
    value: object, field_name: str, *, allow_unavailable_reason: bool = False
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
    if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
        raise AccountAssignmentPerimeterError(f"{field_name} currency is invalid")
    if status == "UNAVAILABLE":
        if amount is not None:
            raise AccountAssignmentPerimeterError(f"{field_name} unavailable value must be null")
        normalized = {"currency": currency, "status": status, "value": None}
        if "reason" in value:
            reason = value["reason"]
            if not isinstance(reason, str) or not reason.strip():
                raise AccountAssignmentPerimeterError(f"{field_name} unavailable reason is invalid")
            normalized["reason"] = reason.strip()
        return normalized
    if status != "AVAILABLE" or not isinstance(amount, str):
        raise AccountAssignmentPerimeterError(f"{field_name} money status is invalid")
    try:
        decimal = Decimal(amount)
    except (InvalidOperation, ValueError):
        raise AccountAssignmentPerimeterError(f"{field_name} value is invalid") from None
    if not decimal.is_finite() or decimal.quantize(Decimal("0.01")) != decimal:
        raise AccountAssignmentPerimeterError(f"{field_name} value is invalid")
    if _CANONICAL_CENT.fullmatch(amount) is None or (
        amount.startswith("-") and decimal.is_zero()
    ):
        raise AccountAssignmentPerimeterError(f"{field_name} value is not canonical")
    return {"currency": currency, "status": status, "value": amount}


def _c3_account_id(value: object, field_name: str) -> str:
    account_id = _nonempty_text(value, field_name)
    if _INTERNAL_ACCOUNT_ID.fullmatch(account_id) is None:
        raise AccountAssignmentPerimeterError(f"{field_name} is invalid")
    return account_id


def _c3_ledger(selected_inputs: AccountManifestSelectedInputs) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]], str]:
    try:
        ledger = selected_inputs.provider_ledger.json_object("selected provider ledger")
    except ValueError:
        raise AccountAssignmentPerimeterError("selected provider ledger is invalid") from None
    _selected_top_level_date(selected_inputs.provider_ledger)
    if not isinstance(ledger.get("catalog_schema_version"), int):
        raise AccountAssignmentPerimeterError("provider ledger catalog schema version is invalid")
    navs, flows = ledger.get("nav_observations"), ledger.get("flows")
    if not isinstance(navs, list) or not isinstance(flows, list):
        raise AccountAssignmentPerimeterError("provider ledger facts must be lists")
    normalized_navs: list[dict[str, object]] = []
    available_currencies: set[str] = set()
    nav_ids: set[str] = set()
    for index, row in enumerate(navs):
        if not isinstance(row, Mapping) or not {
            "record_id", "account_id", "as_of_date", "nav"
        } <= set(row):
            raise AccountAssignmentPerimeterError("provider ledger NAV shape is invalid")
        record_id = _nonempty_text(row.get("record_id"), "NAV record_id")
        if record_id in nav_ids:
            raise AccountAssignmentPerimeterError("duplicate provider ledger NAV record_id")
        nav_ids.add(record_id)
        account_id = _c3_account_id(row.get("account_id"), "NAV account_id")
        as_of_date = _iso_date(row.get("as_of_date"), "NAV as_of_date")
        nav = _c3_money(
            row.get("nav"), f"NAV {index}", allow_unavailable_reason=True
        )
        if nav["status"] == "AVAILABLE":
            available_currencies.add(str(nav["currency"]))
        normalized_navs.append({"record_id": record_id, "account_id": account_id, "as_of_date": as_of_date, "nav": nav})
    if len(available_currencies) != 1:
        raise AccountAssignmentPerimeterError("AVAILABLE NAV facts must use one accounting currency")
    accounting_currency = next(iter(available_currencies))
    normalized_flows: list[dict[str, object]] = []
    flow_ids: set[str] = set()
    for index, row in enumerate(flows):
        if not isinstance(row, Mapping) or not {
            "record_id", "account_id", "event_date", "flow_type", "amount"
        } <= set(row):
            raise AccountAssignmentPerimeterError("provider ledger flow shape is invalid")
        record_id = _nonempty_text(row.get("record_id"), "flow record_id")
        if record_id in flow_ids or any(ord(item) < 32 or ord(item) == 127 for item in record_id):
            raise AccountAssignmentPerimeterError("provider ledger flow record_id is invalid")
        flow_ids.add(record_id)
        account_id = _c3_account_id(row.get("account_id"), "flow account_id")
        event_date = _iso_date(row.get("event_date"), "flow event_date")
        flow_type = _nonempty_text(row.get("flow_type"), "flow type")
        amount = _c3_money(row.get("amount"), f"flow {index}")
        if amount["status"] == "AVAILABLE" and amount["currency"] != accounting_currency:
            raise AccountAssignmentPerimeterError("AVAILABLE flow currency differs from accounting currency")
        normalized_flows.append({"record_id": record_id, "account_id": account_id, "event_date": event_date, "flow_type": flow_type, "amount": amount})
    return ledger, normalized_navs, normalized_flows, accounting_currency


def _c3_mapped_assignments(selected_inputs: AccountManifestSelectedInputs) -> tuple[list[dict[str, object]], dict[str, str]]:
    baseline, providers, assignments = _baseline_payload(selected_inputs.baseline)
    for account_id in providers:
        _c3_account_id(account_id, "baseline account_id")
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            raise AccountAssignmentPerimeterError("baseline assignment is invalid")
        _c3_account_id(assignment.get("account_id"), "baseline assignment account_id")
    try:
        perimeter = partition_assignments(assignments)
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError("baseline assignments are invalid") from exc
    mapped = [dict(item) for chain in perimeter.mapped_chains.values() for item in chain]
    for assignment in mapped:
        assignment["account_id"] = _c3_account_id(
            assignment["account_id"], "mapped assignment account_id"
        )
    mapped.sort(key=lambda item: (str(item["account_id"]), str(item["strategy_id"]), str(item["effective_start"]), str(item["effective_end"])))
    if not mapped:
        raise AccountAssignmentPerimeterError("baseline has no mapped assignments")
    return mapped, providers


def _c3_assignment_record(assignment: Mapping[str, object]) -> dict[str, object]:
    return {key: assignment[key] for key in ("account_id", "strategy_id", "effective_start", "effective_end")}


def _c3_assignment_identity(mapped: list[dict[str, object]]) -> dict[str, object]:
    records = [_c3_assignment_record(item) for item in mapped]
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "count": len(records)}


def _c3_in_interval(value: str, assignment: Mapping[str, object]) -> bool:
    end = assignment["effective_end"]
    return value >= str(assignment["effective_start"]) and (end is None or value <= str(end))


def _c3_rule(
    rules: Mapping[str, object], flow_type: str
) -> Mapping[str, object]:
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
    if len(matches) != 1:
        raise AccountAssignmentPerimeterError(
            "flow classification rule is unresolved or ambiguous"
        )
    return matches[0]


def _c3_classified_flows(
    selected_inputs: AccountManifestSelectedInputs,
    ledger_identity: Mapping[str, object],
    flows: list[dict[str, object]],
) -> tuple[dict[str, object], InputSnapshot, dict[str, object], dict[str, object]]:
    try:
        policy_payload = selected_inputs.flow_classification_policy.json_object("selected flow policy")
    except ValueError:
        raise AccountAssignmentPerimeterError("selected flow policy is invalid") from None
    policy_bytes = canonical_json_bytes(policy_payload)
    policy_snapshot = InputSnapshot.from_bytes("generated policy", policy_bytes)
    try:
        rules, policy_identity = load_flow_classification_policy(policy_payload, policy_snapshot)
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError("selected flow policy is invalid") from exc
    events: list[dict[str, object]] = []
    for flow in flows:
        flow_type = str(flow["flow_type"])
        rule = _c3_rule(rules, flow_type)
        amount = flow["amount"]
        assert isinstance(amount, Mapping)
        if amount["status"] == "UNAVAILABLE":
            event = {"event_id": f"classified-flow:{flow['record_id']}", "source_record_id": flow["record_id"], "account_id": flow["account_id"], "event_date": flow["event_date"], "flow_type": flow_type, "amount": amount, "classification_status": "BLOCKED", "disposition": None, "blocker_code": "AMOUNT_UNAVAILABLE"}
        else:
            value = Decimal(str(amount["value"]))
            sign = rule["amount_sign"]
            if (sign == "POSITIVE" and value <= 0) or (sign == "NEGATIVE" and value >= 0) or (sign == "NONZERO" and value == 0):
                raise AccountAssignmentPerimeterError("flow amount violates policy sign")
            event = {"event_id": f"classified-flow:{flow['record_id']}", "source_record_id": flow["record_id"], "account_id": flow["account_id"], "event_date": flow["event_date"], "flow_type": flow_type, "amount": amount, "classification_status": "CLASSIFIED", "disposition": rule["disposition"], "blocker_code": None}
            if rule["disposition"] == "INTERNAL":
                counterparts = [
                    item
                    for item in flows
                    if item["record_id"] != flow["record_id"]
                    and item["event_date"] == flow["event_date"]
                    and item["account_id"] != flow["account_id"]
                    and isinstance(item["amount"], Mapping)
                    and item["amount"].get("status") == "AVAILABLE"
                    and item["amount"].get("currency") == amount["currency"]
                    and Decimal(str(item["amount"].get("value"))) == -value
                    and _c3_rule(rules, str(item["flow_type"])).get("disposition")
                    == "INTERNAL"
                    and _c3_rule(rules, str(item["flow_type"])).get(
                        "counterparty_rule"
                    )
                    == "SAME_DATE_DIFFERENT_ACCOUNT_EXACT_OPPOSITE_AMOUNT"
                ]
                if len(counterparts) != 1:
                    raise AccountAssignmentPerimeterError(
                        "internal flow must have exactly one reciprocal counterparty"
                    )
                event["counterparty_source_record_id"] = counterparts[0]["record_id"]
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
    payload = {"schema_version": 4, "artifact_id": "authority/classified-flows.json", "as_of_date": selected_inputs.generation_as_of_date.isoformat(), "ledger_identity": ledger_identity, "classification_policy": {"policy_id": policy_identity["policy_id"], "policy_version": policy_identity["policy_version"], "policy_as_of_date": policy_payload["as_of_date"], "policy_sha256": policy_snapshot.sha256}, "events": events}
    snapshot = InputSnapshot.from_bytes("generated classified flows", canonical_json_bytes(payload))
    return payload, snapshot, _c3_artifact(snapshot, "authority/classified-flows.json", "classified-flows", selected_inputs.generation_as_of_date.isoformat(), "4"), policy_identity


def build_blocked_account_manifest_payloads(selected_inputs: AccountManifestSelectedInputs) -> Mapping[str, bytes]:
    """Derive the closed, all-blocked nine-subject authority candidate."""
    if type(selected_inputs) is not AccountManifestSelectedInputs:
        raise AccountAssignmentPerimeterError("selected_inputs must be an AccountManifestSelectedInputs")
    _, navs, flows, accounting_currency = _c3_ledger(selected_inputs)
    mapped, providers = _c3_mapped_assignments(selected_inputs)
    assignment_identity = _c3_assignment_identity(mapped)
    selected_payload = json.loads(build_selected_input_identities(selected_inputs))
    ledger_source = {"generation_id": selected_payload["provider_ledger"]["generation_id"], "artifact": selected_payload["provider_ledger"]["artifact"], "identity_artifact": selected_payload["provider_ledger"]["generation_manifest"]}
    ledger_pair = {"generation_id": ledger_source["generation_id"], "artifact": ledger_source["artifact"]}
    baseline_source = {"generation_id": selected_payload["baseline"]["generation_id"], "artifact": selected_payload["baseline"]["artifact"]}
    classified, classified_snapshot, classified_artifact, policy_identity = _c3_classified_flows(selected_inputs, ledger_pair, flows)
    nav_facts = [{"record_id": item["record_id"], "account_id": item["account_id"], "as_of_date": item["as_of_date"], "nav": {"status": item["nav"]["status"]}} for item in navs]
    raw_facts = [{"record_id": item["record_id"], "account_id": item["account_id"], "event_date": item["event_date"]} for item in flows]
    classified_facts = [{"event_id": item["event_id"], "source_record_id": item["source_record_id"], "account_id": item["account_id"], "event_date": item["event_date"]} for item in classified["events"]]
    perimeter_bytes = build_account_assignment_perimeter(selected_inputs=selected_inputs, nav_facts=nav_facts, raw_flow_facts=raw_facts, classified_flow_facts=classified_facts)
    perimeter_snapshot = InputSnapshot.from_bytes("generated perimeter", perimeter_bytes)
    policy_projection = canonical_json_bytes(selected_inputs.flow_classification_policy.json_object("selected flow policy"))
    policy_snapshot = InputSnapshot.from_bytes("generated policy projection", policy_projection)
    policy_artifact = _c3_artifact(
        policy_snapshot,
        "authority/flow-classification-policy.json",
        "flow-classification-policy",
        str(selected_inputs.flow_classification_policy.json_object("selected flow policy")["as_of_date"]),
    )
    coverage_rows: list[dict[str, object]] = []
    generated_date = selected_inputs.generation_as_of_date.isoformat()
    for assignment in mapped:
        end = assignment["effective_end"] or generated_date
        if str(assignment["effective_start"]) > generated_date or (assignment["effective_end"] is not None and str(assignment["effective_end"]) > generated_date):
            raise AccountAssignmentPerimeterError("mapped assignment lies outside generation date")
        coverage_rows.append({"assignment": _c3_assignment_record(assignment), "requested_start": assignment["effective_start"], "requested_end": end, "status": "BLOCKED", "authority_kind": "NONE", "receipt_artifact_id": None, "blocker_code": "NO_ADMISSIBLE_V2_RECEIPT", "blocker_detail": "No admissible retained receipt issuer."})
    coverage_payload = {"schema_version": 2, "artifact_id": "authority/account-flow-coverage.json", "as_of_date": generated_date, "ledger_identity": ledger_pair, "classified_flows_artifact": classified_artifact, "mapped_assignments_identity": assignment_identity, "receipt_artifacts": [], "rows": coverage_rows}
    coverage_snapshot = InputSnapshot.from_bytes("generated blocked coverage", canonical_json_bytes(coverage_payload))
    try:
        load_flow_coverage(coverage_snapshot, ledger_pair, classified_artifact, {}, mapped)
    except ManifestError as exc:
        raise AccountAssignmentPerimeterError("generated blocked coverage is invalid") from exc
    coverage_artifact = _c3_artifact(coverage_snapshot, "authority/account-flow-coverage.json", "account-flow-coverage", generated_date, "2")
    calculation_rows = [{"epoch": _c3_assignment_record(assignment), "provider": providers[str(assignment["account_id"])], "nav_observations": [item for item in navs if item["account_id"] == assignment["account_id"] and _c3_in_interval(str(item["as_of_date"]), assignment)], "classified_flows": [item for item in classified["events"] if item["account_id"] == assignment["account_id"] and _c3_in_interval(str(item["event_date"]), assignment)]} for assignment in mapped]
    calculation_payload = {"schema_version": 1, "artifact_id": "authority/calculation-inputs.json", "as_of_date": generated_date, "accounting_currency": accounting_currency, "ledger_identity": ledger_source, "baseline_identity": baseline_source, "mapped_assignments_identity": assignment_identity, "rows": calculation_rows}
    calculation_snapshot = InputSnapshot.from_bytes("generated calculation inputs", canonical_json_bytes(json_ready(calculation_payload)))
    calculation_artifact = _c3_artifact(calculation_snapshot, "authority/calculation-inputs.json", "account-manifest-calculation-inputs", generated_date)
    admission_rows = [{"assignment": _c3_assignment_record(assignment), "status": "BLOCKED", "authority_kind": "NONE", "funded_nav_record_ids": [], "continuity_to_successor": False, "blocker_code": "FLOW_COMPLETENESS_NOT_PROVEN"} for assignment in mapped]
    admission_payload = {"schema_version": 1, "artifact_id": "authority/mapped-epoch-admission.json", "as_of_date": generated_date, "ledger_identity": ledger_pair, "coverage_artifact": coverage_artifact, "mapped_assignments_identity": assignment_identity, "rows": admission_rows}
    admission_snapshot = InputSnapshot.from_bytes("generated admission", canonical_json_bytes(admission_payload))
    admission_artifact = _c3_artifact(admission_snapshot, "authority/mapped-epoch-admission.json", "mapped-epoch-admission", generated_date)
    code_payload = {"schema_version": 1, "artifact_id": "authority/code-identities.json", "as_of_date": generated_date, "code_sources": selected_payload["code_sources"]}
    code_snapshot = InputSnapshot.from_bytes("generated code identities", canonical_json_bytes(code_payload))
    code_artifact = _c3_artifact(code_snapshot, "authority/code-identities.json", "account-manifest-code-identities", generated_date)
    output_rows = [{"account_id": assignment["account_id"], "provider": providers[str(assignment["account_id"])], "strategy_id": assignment["strategy_id"], "effective_start": assignment["effective_start"], "effective_end": assignment["effective_end"], "admission_status": "BLOCKED", "admission_policy_reference": "NONE", "admission_authority_kind": "NONE", "funded_nav_record_ids": [], "continuity_to_successor": False, "status": "BLOCKED_ADMISSION", "blocking_reason": "FLOW_COMPLETENESS_NOT_PROVEN"} for assignment in mapped]
    output_payload = {"schema_version": 8, "artifact": "account-strategy-manifest", "accounting_currency": accounting_currency, "authority_readiness": {"authority_status": "SOURCE_BOUND_CANDIDATE", "independent_authority_verification": "NOT_PERFORMED", "publication_readiness": "NOT_READY"}, "source_identities": {"ledger": ledger_source, "baseline": baseline_source, "classified_flows": {"artifact": classified_artifact, "schema_version": 4, "ledger_identity": ledger_source, "classification_policy": {"policy_id": policy_identity["policy_id"], "policy_version": policy_identity["policy_version"], "policy_as_of_date": selected_inputs.flow_classification_policy.json_object("selected flow policy")["as_of_date"], "policy_sha256": policy_snapshot.sha256}}, "flow_classification_policy": {**policy_identity, "artifact": policy_artifact}, "flow_coverage": {"artifact": coverage_artifact, "ledger_identity": ledger_source, "classified_flows_artifact": classified_artifact}, "admission_policy": {"policy_id": "MAPPED_EPOCH_ADMISSION_V1", "policy_version": "1", "artifact": admission_artifact, "assignment": assignment_identity}, "assignment": assignment_identity, "calculation_code": selected_payload["code_sources"]["epoch_producer"], "selected_inputs": _c3_artifact(InputSnapshot.from_bytes("selected identities", canonical_json_bytes(selected_payload)), "authority/selected-input-identities.json", "selected-input-identities", generated_date), "account_assignment_perimeter": _c3_artifact(perimeter_snapshot, "authority/account-assignment-perimeter.json", "account-assignment-perimeter", generated_date), "calculation_inputs": calculation_artifact, "code_identities": code_artifact}, "epoch_continuity": {"rule_id": "EXPLICIT_FUNDED_EPOCH_CONTINUITY_V1", "status": "POLICY_BOUND_ONLY", "rule": "Blocked candidates do not establish funded continuity."}, "flow_reconciliation": {"status": "ONE_TO_ONE_VERIFIED", "method": "EXACT_RAW_RECORD_TO_CLASSIFIED_EVENT_BINDING"}, "rows": output_rows}
    payloads = {"authority/selected-input-identities.json": canonical_json_bytes(selected_payload), "authority/account-assignment-perimeter.json": perimeter_bytes, "authority/calculation-inputs.json": calculation_snapshot.contents, "authority/flow-classification-policy.json": policy_projection, "authority/classified-flows.json": classified_snapshot.contents, "authority/account-flow-coverage.json": coverage_snapshot.contents, "authority/mapped-epoch-admission.json": admission_snapshot.contents, "authority/code-identities.json": code_snapshot.contents, "outputs/account-strategy-manifest.json": canonical_json_bytes(output_payload)}
    return MappingProxyType(dict(payloads))


def validate_blocked_account_manifest_payloads(payloads: Mapping[str, bytes], selected_inputs: AccountManifestSelectedInputs) -> None:
    if not isinstance(payloads, Mapping) or dict(payloads) != dict(build_blocked_account_manifest_payloads(selected_inputs)):
        raise AccountAssignmentPerimeterError("blocked account manifest payloads differ from expected derivation")
