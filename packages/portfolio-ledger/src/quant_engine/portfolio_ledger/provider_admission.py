"""Provider-to-ledger admission contracts.

The contract deliberately separates evidence admission from authority. A fact can be
retained as evidence without being permitted to change account capital, performance,
or readiness. Authority is granted only to an exact provider generation, fact type,
scope, evidence layer, and effective interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import re
from typing import Iterable


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
    ACCEPTED_PARTIAL = "ACCEPTED_PARTIAL"
    FINITE_STATIC = "FINITE_STATIC"


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


def _text(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ProviderAdmissionError(f"{name} must not be empty")
    return normalized


def _hash(value: str, name: str) -> str:
    normalized = value.strip()
    if _SHA256.fullmatch(normalized) is None:
        raise ProviderAdmissionError(f"{name} must be 64 lower-case hex")
    return normalized


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
