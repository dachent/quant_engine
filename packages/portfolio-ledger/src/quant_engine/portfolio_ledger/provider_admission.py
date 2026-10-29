"""Provider-to-ledger admission contracts.

The contract deliberately separates evidence admission from authority. A fact can be
retained as evidence without being permitted to change account capital, performance,
or readiness. Authority is granted only to an exact provider generation, fact type,
scope, evidence layer, and effective interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENT_PROVIDERS = frozenset(
    {
        "alpaca",
        "fidelity",
        "ibkr",
        "matrixport",
        "portfolio123",
        "quantbase",
        "schwab",
    }
)
_DATASET_POLICY_ID = "PROVIDER_ADMIT_OR_DECLARE_V1"
_DATASET_POLICY_VERSION = "1"
_DATASET_POLICY_OWNER = "PRODUCT_OWNER_GOVERNING_ISSUE_303"


class ProviderAdmissionError(ValueError):
    """A provider admission or authority set violates the contract."""


class EvidenceLayer(StrEnum):
    """Permitted evidence layers for current portfolio work."""

    ACCOUNT_LIVE = "ACCOUNT_LIVE"
    PROVIDER_LIVE = "PROVIDER_LIVE"
    OFFICIAL_VEHICLE = "OFFICIAL_VEHICLE"
    PREDECESSOR = "PREDECESSOR"
    SHADOW_OOS = "SHADOW_OOS"
    BACKTEST = "BACKTEST"
    CONCEPTUAL_STRESS = "CONCEPTUAL_STRESS"


class AdmissionDisposition(StrEnum):
    """Whether evidence may enter a consumer and with what force."""

    AUTHORITATIVE = "AUTHORITATIVE"
    RECONCILIATION_ONLY = "RECONCILIATION_ONLY"
    REFERENCE_ONLY = "REFERENCE_ONLY"
    BLOCKED_GAP = "BLOCKED_GAP"


class GenerationQualification(StrEnum):
    """Why an exact provider generation is eligible for bounded admission."""

    PUBLISHED_COMPLETE = "PUBLISHED_COMPLETE"
    STAGED_COMPLETE = "STAGED_COMPLETE"
    ACCEPTED_PARTIAL = "ACCEPTED_PARTIAL"
    FINITE_STATIC = "FINITE_STATIC"


class DatasetItemKind(StrEnum):
    """Kinds of upstream items covered by admit-or-declare."""

    ARTIFACT = "ARTIFACT"
    CATALOG_DATASET = "CATALOG_DATASET"
    COVERAGE_FACT_TYPE = "COVERAGE_FACT_TYPE"
    PRODUCT_DATA_CLASS = "PRODUCT_DATA_CLASS"


class DatasetDispositionAction(StrEnum):
    """The only two valid outcomes for one discovered source item."""

    ADMIT = "ADMIT"
    EXCLUDE = "EXCLUDE"


class SourceAvailability(StrEnum):
    """Whether accepted source observations exist, independent of modeling."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class AnalyticalModelStatus(StrEnum):
    """Validation state of an analytical model using the source observations."""

    NOT_EVALUATED = "NOT_EVALUATED"
    VALIDATED = "VALIDATED"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ProviderAdmissionError(f"{name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ProviderAdmissionError(f"{name} must not be empty")
    return normalized


def _hash(value: object, name: str) -> str:
    normalized = _text(value, name)
    if _SHA256.fullmatch(normalized) is None:
        raise ProviderAdmissionError(f"{name} must be 64 lower-case hex")
    return normalized


def _canonical_json_value_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderAdmissionError(f"{name} must be an object")
    return value


def _sequence(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ProviderAdmissionError(f"{name} must be a list")
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    rows = tuple(_text(item, name) for item in _sequence(value, name))
    if len(set(rows)) != len(rows):
        raise ProviderAdmissionError(f"{name} must be unique")
    return rows


def _date(value: object, name: str) -> date:
    text = _text(value, name)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ProviderAdmissionError(f"{name} is invalid") from exc
    if parsed.isoformat() != text:
        raise ProviderAdmissionError(f"{name} is not canonical")
    return parsed


def _optional_date(value: object, name: str) -> date | None:
    return None if value is None else _date(value, name)


def _utc_timestamp(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderAdmissionError(f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProviderAdmissionError(f"{name} must be timezone-aware UTC")
    return text


@dataclass(frozen=True)
class SourceAnalyticalStatus:
    """Keep data availability distinct from model acceptance."""

    source_availability: SourceAvailability
    analytical_model_status: AnalyticalModelStatus
    analytical_reason: str | None = None

    def __post_init__(self) -> None:
        if self.analytical_reason is not None:
            object.__setattr__(
                self,
                "analytical_reason",
                _text(self.analytical_reason, "analytical_reason"),
            )
        if (
            self.source_availability is SourceAvailability.UNAVAILABLE
            and self.analytical_model_status is not AnalyticalModelStatus.UNAVAILABLE
        ):
            raise ProviderAdmissionError(
                "analytical model cannot be available when source is unavailable"
            )
        if (
            self.analytical_model_status
            in {
                AnalyticalModelStatus.DIAGNOSTIC_ONLY,
                AnalyticalModelStatus.UNAVAILABLE,
            }
            and self.analytical_reason is None
        ):
            raise ProviderAdmissionError(
                "non-validated analytical model status requires a reason"
            )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "analytical_model_status": self.analytical_model_status.value,
            "analytical_reason": self.analytical_reason,
            "source_availability": self.source_availability.value,
        }


@dataclass(frozen=True)
class ProviderGenerationIdentity:
    """The immutable provider inputs that one admission is allowed to read."""

    provider_id: str
    generation_id: str
    manifest_sha256: str
    catalog_sha256: str
    qualification: GenerationQualification

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        object.__setattr__(
            self, "generation_id", _hash(self.generation_id, "generation_id")
        )
        object.__setattr__(
            self,
            "manifest_sha256",
            _hash(self.manifest_sha256, "manifest_sha256"),
        )
        object.__setattr__(
            self, "catalog_sha256", _hash(self.catalog_sha256, "catalog_sha256")
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "catalog_sha256": self.catalog_sha256,
            "generation_id": self.generation_id,
            "manifest_sha256": self.manifest_sha256,
            "provider_id": self.provider_id,
            "qualification": self.qualification.value,
        }


@dataclass(frozen=True)
class ProviderDatasetInventoryItem:
    """One exact item discovered from a verified provider generation."""

    provider_id: str
    item_kind: DatasetItemKind
    item_id: str
    sha256: str
    size_bytes: int
    schema_name: str
    schema_version: str
    record_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        object.__setattr__(self, "item_id", _text(self.item_id, "item_id"))
        object.__setattr__(self, "sha256", _hash(self.sha256, "sha256"))
        object.__setattr__(self, "schema_name", _text(self.schema_name, "schema_name"))
        object.__setattr__(
            self, "schema_version", _text(self.schema_version, "schema_version")
        )
        if self.size_bytes < 0:
            raise ProviderAdmissionError("inventory size_bytes must not be negative")
        if self.record_count is not None and self.record_count < 0:
            raise ProviderAdmissionError("inventory record_count must not be negative")

    @property
    def identity(self) -> tuple[str, DatasetItemKind, str]:
        return (self.provider_id, self.item_kind, self.item_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "item_kind": self.item_kind.value,
            "provider_id": self.provider_id,
            "record_count": self.record_count,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class ProviderDatasetDisposition:
    """One exact admission or exclusion for a discovered source item."""

    provider_id: str
    item_kind: DatasetItemKind
    item_id: str
    item_sha256: str
    action: DatasetDispositionAction
    source_segment_id: str
    source_segment_sha256: str
    authority_ref: str
    readiness_impact: str
    consumers: tuple[str, ...]
    invalidated_outputs: tuple[str, ...]
    evidence_layer: str
    units: str
    currency: str
    timezone: str
    calendar: str
    materialized_record_count: int
    policy_id: str
    policy_version: str
    policy_owner: str
    policy_effective_start: date
    policy_effective_end: date | None
    source_observed_start: date
    source_observed_end: date
    data_class: str | None = None
    materialized_sha256: str | None = None
    observed_start: date | None = None
    observed_end: date | None = None
    target_facts: tuple[str, ...] = ()
    transform: str | None = None
    transform_version: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "provider_id",
            "item_id",
            "source_segment_id",
            "authority_ref",
            "readiness_impact",
            "evidence_layer",
            "units",
            "currency",
            "timezone",
            "calendar",
            "policy_id",
            "policy_version",
            "policy_owner",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(self, "item_sha256", _hash(self.item_sha256, "item_sha256"))
        object.__setattr__(
            self,
            "source_segment_sha256",
            _hash(self.source_segment_sha256, "source_segment_sha256"),
        )
        for field in (
            "data_class",
            "transform",
            "transform_version",
            "reason",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _text(value, field))
        if self.item_kind is DatasetItemKind.PRODUCT_DATA_CLASS:
            if self.data_class != self.item_id:
                raise ProviderAdmissionError(
                    "product data-class disposition identity differs"
                )
        elif self.data_class is not None:
            raise ProviderAdmissionError(
                "non-product dataset disposition cannot declare data_class"
            )
        if self.materialized_record_count < 0:
            raise ProviderAdmissionError(
                "dataset disposition materialized count must not be negative"
            )
        if self.materialized_sha256 is not None:
            object.__setattr__(
                self,
                "materialized_sha256",
                _hash(self.materialized_sha256, "materialized_sha256"),
            )
        if (
            self.policy_effective_end is not None
            and self.policy_effective_end < self.policy_effective_start
        ):
            raise ProviderAdmissionError(
                "dataset disposition policy interval is reversed"
            )
        if self.source_observed_end < self.source_observed_start:
            raise ProviderAdmissionError(
                "dataset disposition source interval is reversed"
            )
        if (
            self.policy_id != _DATASET_POLICY_ID
            or self.policy_version != _DATASET_POLICY_VERSION
            or self.policy_owner != _DATASET_POLICY_OWNER
        ):
            raise ProviderAdmissionError("dataset disposition policy authority differs")
        if (self.observed_start is None) != (self.observed_end is None):
            raise ProviderAdmissionError(
                "dataset disposition observed interval must be complete"
            )
        if (
            self.observed_start is not None
            and self.observed_end is not None
            and self.observed_end < self.observed_start
        ):
            raise ProviderAdmissionError(
                "dataset disposition observed interval is reversed"
            )
        if any(not item.strip() for item in self.consumers):
            raise ProviderAdmissionError("dataset disposition consumer is empty")
        if any(not item.strip() for item in self.target_facts) or len(
            set(self.target_facts)
        ) != len(self.target_facts):
            raise ProviderAdmissionError(
                "dataset disposition target facts must be unique non-empty text"
            )
        if self.action is DatasetDispositionAction.ADMIT:
            if self.readiness_impact != "ADMITTED":
                raise ProviderAdmissionError(
                    "admitted dataset readiness impact must be ADMITTED"
                )
            if not self.consumers:
                raise ProviderAdmissionError(
                    "admitted dataset disposition requires consumers"
                )
            if (
                self.reason is not None
                or any(
                    value is None
                    for value in (
                        self.transform,
                        self.transform_version,
                        self.materialized_sha256,
                        self.observed_start,
                        self.observed_end,
                    )
                )
                or not self.target_facts
            ):
                raise ProviderAdmissionError(
                    "admitted dataset requires transform and target without exclusion reason"
                )
            if self.materialized_record_count <= 0:
                raise ProviderAdmissionError(
                    "admitted dataset requires materialized records"
                )
        else:
            if self.reason is None or not self.invalidated_outputs:
                raise ProviderAdmissionError(
                    "excluded dataset requires reason and invalidated outputs"
                )
            if (
                any(
                    value is not None
                    for value in (
                        self.transform,
                        self.transform_version,
                        self.materialized_sha256,
                        self.observed_start,
                        self.observed_end,
                    )
                )
                or self.target_facts
            ):
                raise ProviderAdmissionError(
                    "excluded dataset cannot declare an admission transform"
                )

    @property
    def identity(self) -> tuple[str, DatasetItemKind, str]:
        return (self.provider_id, self.item_kind, self.item_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "authority_ref": self.authority_ref,
            "consumers": list(self.consumers),
            "currency": self.currency,
            "data_class": self.data_class,
            "evidence_layer": self.evidence_layer,
            "invalidated_outputs": list(self.invalidated_outputs),
            "item_id": self.item_id,
            "item_kind": self.item_kind.value,
            "item_sha256": self.item_sha256,
            "materialized_record_count": self.materialized_record_count,
            "materialized_sha256": self.materialized_sha256,
            "observed_end": (
                self.observed_end.isoformat() if self.observed_end is not None else None
            ),
            "observed_start": (
                self.observed_start.isoformat()
                if self.observed_start is not None
                else None
            ),
            "provider_id": self.provider_id,
            "policy_effective_end": (
                self.policy_effective_end.isoformat()
                if self.policy_effective_end is not None
                else None
            ),
            "policy_effective_start": self.policy_effective_start.isoformat(),
            "policy_id": self.policy_id,
            "policy_owner": self.policy_owner,
            "policy_version": self.policy_version,
            "readiness_impact": self.readiness_impact,
            "reason": self.reason,
            "source_segment_id": self.source_segment_id,
            "source_segment_sha256": self.source_segment_sha256,
            "source_observed_end": self.source_observed_end.isoformat(),
            "source_observed_start": self.source_observed_start.isoformat(),
            "target_facts": list(self.target_facts),
            "timezone": self.timezone,
            "transform": self.transform,
            "transform_version": self.transform_version,
            "units": self.units,
            "calendar": self.calendar,
        }


@dataclass(frozen=True)
class ProviderFactAdmission:
    """Admission and authority for one provider fact inside one exact scope."""

    provider_id: str
    fact_type: str
    evidence_layer: EvidenceLayer
    disposition: AdmissionDisposition
    effective_start: date
    effective_end: date
    account_id: str | None = None
    pool_id: str | None = None
    strategy_id: str | None = None
    account_reconciliation_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        object.__setattr__(self, "fact_type", _text(self.fact_type, "fact_type"))
        if self.effective_end < self.effective_start:
            raise ProviderAdmissionError("provider fact effective interval is reversed")
        scope = (self.account_id, self.pool_id, self.strategy_id)
        if all(value is None for value in scope):
            raise ProviderAdmissionError("provider fact admission requires a scope")
        for name in ("account_id", "pool_id", "strategy_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, name))
        if self.evidence_layer is EvidenceLayer.ACCOUNT_LIVE:
            if self.account_id is None:
                raise ProviderAdmissionError(
                    "ACCOUNT_LIVE evidence requires account_id"
                )
            if self.account_reconciliation_sha256 is None:
                raise ProviderAdmissionError(
                    "ACCOUNT_LIVE evidence requires account reconciliation proof"
                )
            object.__setattr__(
                self,
                "account_reconciliation_sha256",
                _hash(
                    self.account_reconciliation_sha256,
                    "account_reconciliation_sha256",
                ),
            )
        elif self.account_reconciliation_sha256 is not None:
            raise ProviderAdmissionError(
                "non-account evidence cannot contain account reconciliation proof"
            )
        if (
            self.disposition is AdmissionDisposition.AUTHORITATIVE
            and self.evidence_layer is not EvidenceLayer.ACCOUNT_LIVE
            and self.account_id is not None
        ):
            raise ProviderAdmissionError(
                "account authority requires ACCOUNT_LIVE evidence"
            )

    @property
    def scope_key(self) -> tuple[str | None, str | None, str | None]:
        return (self.account_id, self.pool_id, self.strategy_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "account_reconciliation_sha256": self.account_reconciliation_sha256,
            "disposition": self.disposition.value,
            "effective_end": self.effective_end.isoformat(),
            "effective_start": self.effective_start.isoformat(),
            "evidence_layer": self.evidence_layer.value,
            "fact_type": self.fact_type,
            "pool_id": self.pool_id,
            "provider_id": self.provider_id,
            "strategy_id": self.strategy_id,
        }


def validate_provider_authority(
    admissions: Iterable[ProviderFactAdmission],
) -> tuple[ProviderFactAdmission, ...]:
    """Reject duplicate or overlapping authoritative selections.

    Non-authoritative observations intentionally may overlap. This preserves the
    evidence needed to reconcile providers without allowing competing facts to
    silently change portfolio results.
    """

    ordered = tuple(
        sorted(
            admissions,
            key=lambda item: (
                item.fact_type,
                tuple(value or "" for value in item.scope_key),
                item.effective_start,
                item.effective_end,
                item.provider_id,
                item.evidence_layer.value,
                item.disposition.value,
                item.account_reconciliation_sha256 or "",
            ),
        )
    )
    seen: set[tuple[object, ...]] = set()
    authoritative: dict[
        tuple[str, tuple[str | None, str | None, str | None]],
        list[ProviderFactAdmission],
    ] = {}
    for item in ordered:
        identity = (
            item.provider_id,
            item.fact_type,
            item.scope_key,
            item.evidence_layer,
            item.disposition,
            item.effective_start,
            item.effective_end,
            item.account_reconciliation_sha256,
        )
        if identity in seen:
            raise ProviderAdmissionError("provider fact admission is duplicated")
        seen.add(identity)
        if item.disposition is AdmissionDisposition.AUTHORITATIVE:
            authoritative.setdefault((item.fact_type, item.scope_key), []).append(item)

    for scoped in authoritative.values():
        for left, right in zip(scoped, scoped[1:]):
            if right.effective_start <= left.effective_end:
                raise ProviderAdmissionError(
                    "authoritative provider fact intervals overlap"
                )
    return ordered


def validate_dataset_dispositions(
    inventory: Iterable[ProviderDatasetInventoryItem],
    dispositions: Iterable[ProviderDatasetDisposition],
) -> tuple[
    tuple[ProviderDatasetInventoryItem, ...], tuple[ProviderDatasetDisposition, ...]
]:
    """Require one and only one exact disposition for every discovered item."""

    ordered_inventory = tuple(
        sorted(
            inventory,
            key=lambda item: (item.provider_id, item.item_kind.value, item.item_id),
        )
    )
    ordered_dispositions = tuple(
        sorted(
            dispositions,
            key=lambda item: (item.provider_id, item.item_kind.value, item.item_id),
        )
    )
    inventory_by_identity: dict[
        tuple[str, DatasetItemKind, str], ProviderDatasetInventoryItem
    ] = {}
    for inventory_item in ordered_inventory:
        if inventory_item.identity in inventory_by_identity:
            raise ProviderAdmissionError("provider dataset inventory is duplicated")
        inventory_by_identity[inventory_item.identity] = inventory_item
    disposition_by_identity: dict[
        tuple[str, DatasetItemKind, str], ProviderDatasetDisposition
    ] = {}
    for disposition in ordered_dispositions:
        if disposition.identity in disposition_by_identity:
            raise ProviderAdmissionError("provider dataset disposition is duplicated")
        disposition_by_identity[disposition.identity] = disposition
    if set(inventory_by_identity) != set(disposition_by_identity):
        missing = sorted(set(inventory_by_identity) - set(disposition_by_identity))
        extra = sorted(set(disposition_by_identity) - set(inventory_by_identity))
        raise ProviderAdmissionError(
            f"provider dataset disposition coverage differs; missing={missing}; extra={extra}"
        )
    for identity, disposition in disposition_by_identity.items():
        if disposition.item_sha256 != inventory_by_identity[identity].sha256:
            raise ProviderAdmissionError(
                f"provider dataset disposition hash differs: {identity}"
            )
    return ordered_inventory, ordered_dispositions


def provider_upstream_inventory_digest(
    inventory: Iterable[ProviderDatasetInventoryItem],
) -> str:
    """Hash the complete upstream inventory without circular product rows."""

    rows = [
        item.to_dict()
        for item in sorted(
            inventory, key=lambda item: (item.item_kind.value, item.item_id)
        )
        if item.item_kind is not DatasetItemKind.PRODUCT_DATA_CLASS
    ]
    return hashlib.sha256(_canonical_json_value_bytes(rows)).hexdigest()


def product_data_class_set_digest(
    inventory: Iterable[ProviderDatasetInventoryItem],
) -> tuple[int, str]:
    """Hash the exact product data-class key set without source-hash cycles."""

    rows = sorted(
        [
            {
                "item_id": item.item_id,
                "item_kind": item.item_kind.value,
                "item_sha256": item.sha256,
            }
            for item in inventory
            if item.item_kind is DatasetItemKind.PRODUCT_DATA_CLASS
        ],
        key=lambda item: item["item_id"],
    )
    return len(rows), hashlib.sha256(_canonical_json_value_bytes(rows)).hexdigest()


def admitted_binding_set_digest(
    dispositions: Iterable[ProviderDatasetDisposition],
) -> tuple[int, str]:
    """Hash every admitted item together with its exact governed binding."""

    rows = sorted(
        [
            {
                "binding_class": item.evidence_layer,
                "calendar": item.calendar,
                "consumers": list(item.consumers),
                "currency": item.currency,
                "data_class": item.data_class,
                "item_id": item.item_id,
                "item_kind": item.item_kind.value,
                "item_sha256": item.item_sha256,
                "materialized_record_count": item.materialized_record_count,
                "materialized_sha256": item.materialized_sha256,
                "observed_end": (
                    item.observed_end.isoformat()
                    if item.observed_end is not None
                    else None
                ),
                "observed_start": (
                    item.observed_start.isoformat()
                    if item.observed_start is not None
                    else None
                ),
                "readiness_impact": item.readiness_impact,
                "target_facts": list(item.target_facts),
                "timezone": item.timezone,
                "transform": item.transform,
                "transform_version": item.transform_version,
                "units": item.units,
            }
            for item in dispositions
            if item.action is DatasetDispositionAction.ADMIT
        ],
        key=lambda item: (item["item_kind"], item["item_id"]),
    )
    return len(rows), hashlib.sha256(_canonical_json_value_bytes(rows)).hexdigest()


def _exclusion_readiness_impact(
    policy: Mapping[str, Any], source: Mapping[str, Any], reason: str
) -> str:
    """Mirror of tools/provider_ledger.py::_exclusion_readiness_impact.

    The verifier must resolve an exclusion's readiness exactly as the builder
    did, or a correctly built receipt fails verification.
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


def validate_admit_or_declare_receipt(
    value: Mapping[str, object],
    *,
    product_authority: Mapping[str, object],
    source_segments: Mapping[str, Mapping[str, object]],
    provider_generations: Mapping[str, Mapping[str, object]],
) -> tuple[
    tuple[ProviderDatasetInventoryItem, ...], tuple[ProviderDatasetDisposition, ...]
]:
    """Validate a receipt against the exact accepted product source authority."""

    expected = {
        "admission_gate_status",
        "census_id",
        "census_sha256",
        "contract_raw_sha256",
        "contract_semantic_sha256",
        "disposition_sha256",
        "dispositions",
        "invalidated_outputs",
        "inventory",
        "inventory_sha256",
        "product_id",
        "product_readiness",
        "product_version",
        "provider_generations",
        "receipt_type",
        "schema_version",
    }
    has_run_observation = "run_observation" in value
    if set(value) != (
        expected | ({"run_observation"} if has_run_observation else set())
    ):
        raise ProviderAdmissionError("provider admission receipt keys differ")
    if value["admission_gate_status"] != "PASS":
        raise ProviderAdmissionError("provider admission gate did not pass")
    if value["receipt_type"] != "PROVIDER_ADMIT_OR_DECLARE_V1":
        raise ProviderAdmissionError("provider admission receipt type differs")
    if value["schema_version"] != 1:
        raise ProviderAdmissionError("provider admission receipt schema differs")
    if value["product_id"] != "FINANCIAL_MVP":
        raise ProviderAdmissionError("provider admission product differs")
    _text(value["census_id"], "census_id")
    _hash(value["census_sha256"], "census_sha256")
    _hash(value["contract_raw_sha256"], "contract_raw_sha256")
    _hash(value["contract_semantic_sha256"], "contract_semantic_sha256")
    _text(value["product_version"], "product_version")
    for field in (
        "census_id",
        "census_sha256",
        "contract_raw_sha256",
        "contract_semantic_sha256",
        "product_id",
        "product_version",
    ):
        if value[field] != product_authority.get(field):
            raise ProviderAdmissionError(
                "provider receipt product authority identity differs"
            )

    provider_keys = {
        "adapter_name",
        "adapter_version",
        "catalog_sha256",
        "coverage_boundary",
        "coverage_status",
        "generation_as_of_date",
        "generation_id",
        "manifest_sha256",
        "observed_end",
        "observed_start",
        "provider_id",
        "qualification",
        "source_segment_id",
        "source_segment_sha256",
    }
    provider_ids: set[str] = set()
    source_ids: set[str] = set()
    provider_rows: dict[str, Mapping[str, object]] = {}
    for raw in _sequence(value["provider_generations"], "provider_generations"):
        row = _mapping(raw, "provider_generation")
        if set(row) != provider_keys:
            raise ProviderAdmissionError("provider generation receipt keys differ")
        provider_id = _text(row["provider_id"], "provider_id")
        source_id = _text(row["source_segment_id"], "source_segment_id")
        if provider_id in provider_ids or source_id in source_ids:
            raise ProviderAdmissionError("provider generation receipt is duplicated")
        provider_ids.add(provider_id)
        source_ids.add(source_id)
        provider_rows[provider_id] = row
        for field in (
            "catalog_sha256",
            "generation_id",
            "manifest_sha256",
            "source_segment_sha256",
        ):
            _hash(row[field], field)
        for field in ("adapter_name", "adapter_version", "coverage_boundary"):
            _text(row[field], field)
        if row["coverage_status"] not in {
            "COMPLETE_WITHIN_DECLARED_BOUNDARY",
            "PARTIAL",
            "FAILED",
        }:
            raise ProviderAdmissionError("provider coverage status differs")
        _date(row["generation_as_of_date"], "generation_as_of_date")
        observed_start = _date(row["observed_start"], "observed_start")
        observed_end = _date(row["observed_end"], "observed_end")
        if observed_end < observed_start:
            raise ProviderAdmissionError("provider observed interval is reversed")
        try:
            GenerationQualification(_text(row["qualification"], "qualification"))
        except ValueError as exc:
            raise ProviderAdmissionError(
                "provider generation qualification differs"
            ) from exc
    if provider_ids != _CURRENT_PROVIDERS:
        raise ProviderAdmissionError("provider generation set differs")
    if set(provider_generations) != _CURRENT_PROVIDERS:
        raise ProviderAdmissionError("trusted provider generation set differs")
    for provider_id, provider_row in provider_rows.items():
        trusted = _mapping(
            provider_generations[provider_id], "trusted provider generation"
        )
        if provider_row != trusted:
            raise ProviderAdmissionError(
                "provider generation receipt differs from trusted authority"
            )

    observation_rows: dict[str, Mapping[str, object]] = {}
    observation_cutoff: date | None = None
    if has_run_observation:
        observation = _mapping(value["run_observation"], "run observation")
        if set(observation) != {
            "captured_at_utc",
            "financial_cutoff",
            "path",
            "policy_contract_sha256",
            "providers",
            "sha256",
            "size_bytes",
        }:
            raise ProviderAdmissionError("run observation receipt keys differ")
        _utc_timestamp(observation["captured_at_utc"], "run observation capture")
        observation_cutoff = _date(
            observation["financial_cutoff"], "run observation cutoff"
        )
        if observation["policy_contract_sha256"] != value["contract_raw_sha256"]:
            raise ProviderAdmissionError("run observation policy contract differs")
        _hash(observation["sha256"], "run observation sha256")
        _text(observation["path"], "run observation path")
        if (
            isinstance(observation["size_bytes"], bool)
            or not isinstance(observation["size_bytes"], int)
            or observation["size_bytes"] < 1
        ):
            raise ProviderAdmissionError("run observation size differs")
        observation_provider_keys = {
            "catalog_sha256",
            "finality_evidence",
            "finality_interpretation",
            "finalized_through",
            "generation_as_of_date",
            "generation_id",
            "historical_admitted_item_count",
            "historical_admitted_item_set_sha256",
            "historical_inventory_sha256",
            "manifest_sha256",
            "observed_admitted_item_count",
            "observed_admitted_item_set_sha256",
            "observed_inventory_count",
            "observed_inventory_sha256",
            "provider_id",
            "qualification",
        }
        for raw in _sequence(observation["providers"], "run observation providers"):
            row = _mapping(raw, "run observation provider")
            if set(row) != observation_provider_keys:
                raise ProviderAdmissionError(
                    "run observation provider receipt keys differ"
                )
            provider_id = _text(row["provider_id"], "run observation provider_id")
            if provider_id in observation_rows:
                raise ProviderAdmissionError("run observation provider is duplicated")
            observation_rows[provider_id] = row
            observed_provider_row = provider_rows.get(provider_id)
            if observed_provider_row is None:
                raise ProviderAdmissionError(
                    "run observation provider identity differs"
                )
            if any(
                row[field] != observed_provider_row[field]
                for field in (
                    "catalog_sha256",
                    "generation_as_of_date",
                    "generation_id",
                    "manifest_sha256",
                    "qualification",
                )
            ):
                raise ProviderAdmissionError(
                    "run observation provider identity differs"
                )
            # A run observation seals every selected provider identity and its
            # source-finality evidence.  It does not recast the selected
            # generation: published, finite-static, and accepted-partial
            # qualifications retain their original admission semantics.
            GenerationQualification(_text(row["qualification"], "qualification"))
            _date(row["finalized_through"], "run observation finality")
            interpretation = _mapping(
                row["finality_interpretation"], "source finality interpretation"
            )
            if (
                set(interpretation)
                != {
                    "basis",
                    "provider_finality_guarantee",
                    "native_timezone",
                    "native_calendar",
                    "capture_start_utc",
                    "capture_end_utc",
                }
                or interpretation["provider_finality_guarantee"] is not False
            ):
                raise ProviderAdmissionError("source finality interpretation differs")
            _text(interpretation["basis"], "source finality basis")
            for field in ("native_timezone", "native_calendar"):
                if interpretation[field] is not None:
                    _text(interpretation[field], field)
            for field in ("capture_start_utc", "capture_end_utc"):
                if interpretation[field] is not None:
                    _utc_timestamp(interpretation[field], field)
            finality = _mapping(
                row["finality_evidence"], "run observation finality evidence"
            )
            if set(finality) != {"path", "sha256", "size_bytes"}:
                raise ProviderAdmissionError("run observation finality binding differs")
            _text(finality["path"], "run observation finality path")
            _hash(finality["sha256"], "run observation finality sha256")
            if (
                isinstance(finality["size_bytes"], bool)
                or not isinstance(finality["size_bytes"], int)
                or finality["size_bytes"] < 1
            ):
                raise ProviderAdmissionError("run observation finality size differs")
            for field in (
                "historical_admitted_item_set_sha256",
                "historical_inventory_sha256",
                "observed_admitted_item_set_sha256",
                "observed_inventory_sha256",
            ):
                _hash(row[field], field)
            for field in (
                "historical_admitted_item_count",
                "observed_admitted_item_count",
                "observed_inventory_count",
            ):
                if (
                    isinstance(row[field], bool)
                    or not isinstance(row[field], int)
                    or row[field] < 0
                ):
                    raise ProviderAdmissionError("run observation count differs")
        if set(observation_rows) != _CURRENT_PROVIDERS:
            raise ProviderAdmissionError("run observation provider set differs")

    inventory_keys = {
        "item_id",
        "item_kind",
        "provider_id",
        "record_count",
        "schema_name",
        "schema_version",
        "sha256",
        "size_bytes",
    }
    inventory = []
    inventory_rows = _sequence(value["inventory"], "inventory")
    for raw in inventory_rows:
        row = _mapping(raw, "inventory item")
        if set(row) != inventory_keys:
            raise ProviderAdmissionError("provider inventory item keys differ")
        try:
            item_kind = DatasetItemKind(_text(row["item_kind"], "item_kind"))
        except ValueError as exc:
            raise ProviderAdmissionError(
                "provider inventory item kind differs"
            ) from exc
        size_bytes = row["size_bytes"]
        record_count = row["record_count"]
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise ProviderAdmissionError("provider inventory size is invalid")
        if record_count is not None and (
            isinstance(record_count, bool) or not isinstance(record_count, int)
        ):
            raise ProviderAdmissionError("provider inventory record count is invalid")
        inventory.append(
            ProviderDatasetInventoryItem(
                provider_id=_text(row["provider_id"], "provider_id"),
                item_kind=item_kind,
                item_id=_text(row["item_id"], "item_id"),
                sha256=_hash(row["sha256"], "sha256"),
                size_bytes=size_bytes,
                schema_name=_text(row["schema_name"], "schema_name"),
                schema_version=_text(row["schema_version"], "schema_version"),
                record_count=record_count,
            )
        )

    disposition_keys = {
        "action",
        "authority_ref",
        "calendar",
        "consumers",
        "currency",
        "data_class",
        "evidence_layer",
        "invalidated_outputs",
        "item_id",
        "item_kind",
        "item_sha256",
        "materialized_record_count",
        "materialized_sha256",
        "observed_end",
        "observed_start",
        "policy_effective_end",
        "policy_effective_start",
        "policy_id",
        "policy_owner",
        "policy_version",
        "provider_id",
        "readiness_impact",
        "reason",
        "source_observed_end",
        "source_observed_start",
        "source_segment_id",
        "source_segment_sha256",
        "target_facts",
        "timezone",
        "transform",
        "transform_version",
        "units",
    }
    dispositions = []
    disposition_rows = _sequence(value["dispositions"], "dispositions")
    for raw in disposition_rows:
        row = _mapping(raw, "dataset disposition")
        if set(row) != disposition_keys:
            raise ProviderAdmissionError("provider dataset disposition keys differ")
        try:
            item_kind = DatasetItemKind(_text(row["item_kind"], "item_kind"))
            action = DatasetDispositionAction(_text(row["action"], "action"))
        except ValueError as exc:
            raise ProviderAdmissionError(
                "provider dataset disposition enum differs"
            ) from exc
        materialized_count = row["materialized_record_count"]
        if isinstance(materialized_count, bool) or not isinstance(
            materialized_count, int
        ):
            raise ProviderAdmissionError(
                "provider dataset materialized count is invalid"
            )
        dispositions.append(
            ProviderDatasetDisposition(
                provider_id=_text(row["provider_id"], "provider_id"),
                item_kind=item_kind,
                item_id=_text(row["item_id"], "item_id"),
                item_sha256=_hash(row["item_sha256"], "item_sha256"),
                action=action,
                source_segment_id=_text(row["source_segment_id"], "source_segment_id"),
                source_segment_sha256=_hash(
                    row["source_segment_sha256"], "source_segment_sha256"
                ),
                authority_ref=_text(row["authority_ref"], "authority_ref"),
                readiness_impact=_text(row["readiness_impact"], "readiness_impact"),
                consumers=_strings(row["consumers"], "consumers"),
                invalidated_outputs=_strings(
                    row["invalidated_outputs"], "invalidated_outputs"
                ),
                evidence_layer=_text(row["evidence_layer"], "evidence_layer"),
                units=_text(row["units"], "units"),
                currency=_text(row["currency"], "currency"),
                timezone=_text(row["timezone"], "timezone"),
                calendar=_text(row["calendar"], "calendar"),
                materialized_record_count=materialized_count,
                policy_id=_text(row["policy_id"], "policy_id"),
                policy_version=_text(row["policy_version"], "policy_version"),
                policy_owner=_text(row["policy_owner"], "policy_owner"),
                policy_effective_start=_date(
                    row["policy_effective_start"], "policy_effective_start"
                ),
                policy_effective_end=_optional_date(
                    row["policy_effective_end"], "policy_effective_end"
                ),
                source_observed_start=_date(
                    row["source_observed_start"], "source_observed_start"
                ),
                source_observed_end=_date(
                    row["source_observed_end"], "source_observed_end"
                ),
                data_class=(
                    None
                    if row["data_class"] is None
                    else _text(row["data_class"], "data_class")
                ),
                materialized_sha256=(
                    None
                    if row["materialized_sha256"] is None
                    else _hash(row["materialized_sha256"], "materialized_sha256")
                ),
                observed_start=_optional_date(row["observed_start"], "observed_start"),
                observed_end=_optional_date(row["observed_end"], "observed_end"),
                target_facts=_strings(row["target_facts"], "target_facts"),
                transform=(
                    None
                    if row["transform"] is None
                    else _text(row["transform"], "transform")
                ),
                transform_version=(
                    None
                    if row["transform_version"] is None
                    else _text(row["transform_version"], "transform_version")
                ),
                reason=(
                    None if row["reason"] is None else _text(row["reason"], "reason")
                ),
            )
        )

    ordered_inventory, ordered_dispositions = validate_dataset_dispositions(
        inventory, dispositions
    )
    if {item.provider_id for item in ordered_inventory} - _CURRENT_PROVIDERS:
        raise ProviderAdmissionError("provider inventory contains an unknown provider")
    contract_semantic_sha256 = _hash(
        value["contract_semantic_sha256"], "contract_semantic_sha256"
    )
    for item in ordered_dispositions:
        provider_row = provider_rows[item.provider_id]
        source_segment_id = _text(
            provider_row["source_segment_id"], "source_segment_id"
        )
        source_segment_sha256 = _hash(
            provider_row["source_segment_sha256"], "source_segment_sha256"
        )
        expected_authority_ref = (
            f"contract:{contract_semantic_sha256}#source_segments/"
            f"{source_segment_id};issue:303"
        )
        source_raw = source_segments.get(source_segment_id)
        if source_raw is None:
            raise ProviderAdmissionError(
                "provider receipt references an unknown product source segment"
            )
        source = _mapping(source_raw, "product source segment")
        if source.get("source_id") != source_segment_id:
            raise ProviderAdmissionError("product source segment identity differs")
        expected_source_sha256 = hashlib.sha256(
            _canonical_json_value_bytes(source)
        ).hexdigest()
        generation = _mapping(
            source.get("current_generation"), "source current_generation"
        )
        units = _mapping(source.get("units"), "source units")
        temporal = _mapping(source.get("temporal"), "source temporal")
        admission = _mapping(source.get("admission"), "source admission")
        policy = _mapping(
            admission.get("dataset_disposition_policy"),
            "source dataset disposition policy",
        )
        expected_consumers = tuple(
            sorted(_strings(source.get("consumers"), "source consumers"))
        )
        expected_invalidated = tuple(
            sorted(_strings(source.get("invalidated_outputs"), "invalidated outputs"))
        ) or ("NO_CURRENT_PRODUCT_OUTPUT",)
        # A run observation limits consumed facts, not the immutable capture
        # envelope recorded by the selected generation.  In particular a
        # Matrixport Sep18 capture may legitimately feed a Sep17 cutoff.
        expected_source_observed_end = _date(
            provider_row["observed_end"], "observed_end"
        )
        if (
            item.source_segment_id != source_segment_id
            or item.source_segment_sha256 != source_segment_sha256
            or source_segment_sha256 != expected_source_sha256
            or item.authority_ref != expected_authority_ref
            or (
                not has_run_observation
                and (
                    generation.get("generation_id") != provider_row["generation_id"]
                    or generation.get("sha256") != provider_row["manifest_sha256"]
                    or generation.get("as_of") != provider_row["generation_as_of_date"]
                )
            )
            or item.evidence_layer != source.get("evidence_layer")
            or item.consumers != expected_consumers
            or item.units != units.get("measurement")
            or item.currency != units.get("currency")
            or item.timezone != temporal.get("timezone")
            or item.calendar != temporal.get("calendar")
            or item.source_observed_start
            != _date(provider_row["observed_start"], "observed_start")
            or item.source_observed_end != expected_source_observed_end
        ):
            raise ProviderAdmissionError(
                "provider disposition authority differs from provider generation"
            )
        if (
            item.policy_id != policy.get("policy_id")
            or item.policy_version != policy.get("policy_version")
            or item.policy_owner != policy.get("policy_owner")
            or item.policy_effective_start
            != _date(policy.get("effective_start"), "policy effective_start")
            or item.policy_effective_end
            != _optional_date(policy.get("effective_end"), "policy effective_end")
        ):
            raise ProviderAdmissionError(
                "provider disposition policy differs from product authority"
            )
        generation_as_of = (
            observation_cutoff
            if has_run_observation
            else _date(provider_row["generation_as_of_date"], "generation_as_of_date")
        )
        assert generation_as_of is not None
        if item.policy_effective_start > generation_as_of or (
            item.policy_effective_end is not None
            and item.policy_effective_end < generation_as_of
        ):
            raise ProviderAdmissionError(
                "provider disposition policy does not cover provider generation"
            )
        if item.action is DatasetDispositionAction.ADMIT:
            if (
                item.transform != admission.get("transform")
                or item.transform_version != admission.get("version")
                or item.readiness_impact != "ADMITTED"
                or item.invalidated_outputs
            ):
                raise ProviderAdmissionError(
                    "provider admission binding differs from product authority"
                )
        elif (
            item.reason
            not in set(
                _strings(
                    policy.get("allowed_exclusion_reasons"),
                    "allowed exclusion reasons",
                )
            )
            or item.readiness_impact
            != _exclusion_readiness_impact(policy, source, item.reason or "")
            or item.invalidated_outputs != expected_invalidated
        ):
            raise ProviderAdmissionError(
                "provider exclusion differs from product authority"
            )
    for provider_id in sorted(_CURRENT_PROVIDERS):
        provider_inventory = [
            item for item in ordered_inventory if item.provider_id == provider_id
        ]
        provider_dispositions = [
            item for item in ordered_dispositions if item.provider_id == provider_id
        ]
        source_id = _text(
            provider_rows[provider_id]["source_segment_id"], "source_segment_id"
        )
        source = _mapping(source_segments[source_id], "product source segment")
        admission = _mapping(source.get("admission"), "source admission")
        policy = _mapping(
            admission.get("dataset_disposition_policy"),
            "source dataset disposition policy",
        )
        admitted_count, admitted_digest = admitted_binding_set_digest(
            provider_dispositions
        )
        product_count, product_digest = product_data_class_set_digest(
            provider_inventory
        )
        inventory_digest = provider_upstream_inventory_digest(provider_inventory)
        if has_run_observation:
            observation_row = observation_rows[provider_id]
            if (
                len(provider_inventory) != observation_row["observed_inventory_count"]
                or inventory_digest != observation_row["observed_inventory_sha256"]
                or admitted_count != observation_row["observed_admitted_item_count"]
                or admitted_digest
                != observation_row["observed_admitted_item_set_sha256"]
            ):
                raise ProviderAdmissionError(
                    "run observation receipt counts or digests differ"
                )
            if (
                observation_row["historical_inventory_sha256"]
                != policy.get("upstream_inventory_sha256")
                or observation_row["historical_admitted_item_count"]
                != policy.get("admitted_item_count")
                or observation_row["historical_admitted_item_set_sha256"]
                != policy.get("admitted_item_set_sha256")
            ):
                raise ProviderAdmissionError(
                    "run observation historical comparison differs"
                )
            if product_count != policy.get(
                "product_data_class_count"
            ) or product_digest != policy.get("product_data_class_set_sha256"):
                raise ProviderAdmissionError(
                    "provider receipt product data-class set differs from product authority"
                )
        elif (
            inventory_digest != policy.get("upstream_inventory_sha256")
            or product_count != policy.get("product_data_class_count")
            or product_digest != policy.get("product_data_class_set_sha256")
            or admitted_count != policy.get("admitted_item_count")
            or admitted_digest != policy.get("admitted_item_set_sha256")
        ):
            raise ProviderAdmissionError(
                "provider receipt binding set differs from product authority"
            )
    if hashlib.sha256(_canonical_json_value_bytes(inventory_rows)).hexdigest() != _hash(
        value["inventory_sha256"], "inventory_sha256"
    ):
        raise ProviderAdmissionError("provider inventory digest differs")
    if hashlib.sha256(
        _canonical_json_value_bytes(disposition_rows)
    ).hexdigest() != _hash(value["disposition_sha256"], "disposition_sha256"):
        raise ProviderAdmissionError("provider disposition digest differs")
    blocked = [
        item
        for item in ordered_dispositions
        if item.action is DatasetDispositionAction.EXCLUDE
        and item.readiness_impact == "REQUIRED_NOT_READY"
    ]
    expected_readiness = "REQUIRED_NOT_READY" if blocked else "READY_FOR_COMPOSITION"
    if value["product_readiness"] != expected_readiness:
        raise ProviderAdmissionError("provider product readiness differs")
    expected_invalidated_outputs = sorted(
        {output for item in blocked for output in item.invalidated_outputs}
    )
    if list(_strings(value["invalidated_outputs"], "invalidated_outputs")) != (
        expected_invalidated_outputs
    ):
        raise ProviderAdmissionError("provider invalidated outputs differ")
    return ordered_inventory, ordered_dispositions


# Identity keys name which entity or generation a provider-live block was read from. The
# remaining keys are the measured path itself; two strategies cannot honestly measure the
# same path, so a shared path is a copied block, whatever identity it carries.
PROVIDER_LIVE_BLOCK_IDENTITY_KEYS = frozenset(
    {
        "entity_id",
        "generation_as_of_date",
        "generation_id",
        "model_entity_id",
        "provider_id",
    }
)


def shared_provider_live_blocks(
    rows: Iterable[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    """Return strategy id pairs whose provider-live blocks carry the same measured path."""

    owners: dict[bytes, str] = {}
    shared: list[tuple[str, str]] = []
    for row in rows:
        block = row.get("provider")
        if not isinstance(block, Mapping):
            continue
        measured = {
            key: value
            for key, value in block.items()
            if key not in PROVIDER_LIVE_BLOCK_IDENTITY_KEYS
        }
        if not measured:
            continue
        fingerprint = _canonical_json_value_bytes(measured)
        strategy_id = str(row.get("strategy_id"))
        owner = owners.setdefault(fingerprint, strategy_id)
        if owner != strategy_id:
            shared.append((owner, strategy_id))
    return shared
