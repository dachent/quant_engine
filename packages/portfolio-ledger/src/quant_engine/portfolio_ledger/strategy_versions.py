"""Effective-dated strategy versions and cross-vintage comparison controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import re

from quant_engine.core import EvidenceLayer

from quant_engine.portfolio_ledger.models import LedgerValidationError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StrategyVersionDisposition(StrEnum):
    """Strength of the evidence supporting one strategy version."""

    EVIDENCED = "EVIDENCED"
    REFERENCE_ONLY = "REFERENCE_ONLY"
    UNRESOLVED = "UNRESOLVED"


class VersionMatchStatus(StrEnum):
    MATCHED = "MATCHED"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    UNRESOLVED_VERSION = "UNRESOLVED_VERSION"
    STRATEGY_MISMATCH = "STRATEGY_MISMATCH"


def _text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise LedgerValidationError(f"{field_name} must not be empty")
    return normalized


def _optional_text(value: str | None, field_name: str) -> str | None:
    return None if value is None else _text(value, field_name)


def _optional_hash(value: str | None, field_name: str) -> str | None:
    if value is not None and _SHA256.fullmatch(value) is None:
        raise LedgerValidationError(f"{field_name} must be 64 lower-case hex")
    return value


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    version_ref: str | None
    disposition: StrategyVersionDisposition
    effective_start: date | None
    effective_end: date | None
    design_artifact_sha256: str | None
    backtest_applicable: bool
    backtest_artifact_sha256: str | None
    provider_entity_fingerprint: str | None
    provider_configuration_sha256: str | None
    assumptions_version: str | None
    cost_model_version: str | None
    gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_id", _text(self.strategy_id, "strategy_id"))
        object.__setattr__(
            self, "version_ref", _optional_text(self.version_ref, "version_ref")
        )
        for field_name in (
            "design_artifact_sha256",
            "backtest_artifact_sha256",
            "provider_entity_fingerprint",
            "provider_configuration_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_hash(getattr(self, field_name), field_name),
            )
        for field_name in ("assumptions_version", "cost_model_version"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        gaps = tuple(sorted({_text(item, "version gap") for item in self.gaps}))
        object.__setattr__(self, "gaps", gaps)
        if (
            self.effective_start is not None
            and self.effective_end is not None
            and self.effective_end < self.effective_start
        ):
            raise LedgerValidationError(
                "strategy version effective interval is reversed"
            )
        if self.disposition is StrategyVersionDisposition.EVIDENCED:
            required = (
                self.version_ref,
                self.effective_start,
                self.design_artifact_sha256,
                self.provider_entity_fingerprint,
                self.provider_configuration_sha256,
                self.assumptions_version,
                self.cost_model_version,
            )
            if any(item is None for item in required):
                raise LedgerValidationError(
                    "EVIDENCED strategy version requires complete design identity"
                )
            if self.backtest_applicable and self.backtest_artifact_sha256 is None:
                raise LedgerValidationError(
                    "applicable backtest requires an artifact hash"
                )
            if (
                not self.backtest_applicable
                and self.backtest_artifact_sha256 is not None
            ):
                raise LedgerValidationError(
                    "non-applicable backtest cannot contain an artifact hash"
                )
        elif self.disposition is StrategyVersionDisposition.REFERENCE_ONLY:
            if self.version_ref is None or not gaps:
                raise LedgerValidationError(
                    "REFERENCE_ONLY strategy version requires a reference and gaps"
                )
        elif self.version_ref is not None or not gaps:
            raise LedgerValidationError(
                "UNRESOLVED strategy version requires a null reference and explicit gaps"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "assumptions_version": self.assumptions_version,
            "backtest_applicable": self.backtest_applicable,
            "backtest_artifact_sha256": self.backtest_artifact_sha256,
            "cost_model_version": self.cost_model_version,
            "design_artifact_sha256": self.design_artifact_sha256,
            "disposition": self.disposition.value,
            "effective_end": (
                self.effective_end.isoformat()
                if self.effective_end is not None
                else None
            ),
            "effective_start": (
                self.effective_start.isoformat()
                if self.effective_start is not None
                else None
            ),
            "gaps": list(self.gaps),
            "provider_configuration_sha256": self.provider_configuration_sha256,
            "provider_entity_fingerprint": self.provider_entity_fingerprint,
            "strategy_id": self.strategy_id,
            "version_ref": self.version_ref,
        }


@dataclass(frozen=True)
class StrategyEvidenceVersion:
    strategy_id: str
    version_ref: str | None
    evidence_layer: EvidenceLayer
    artifact_sha256: str
    observed_start: date
    observed_end: date

    def __post_init__(self) -> None:
        object.__setattr__(self, "strategy_id", _text(self.strategy_id, "strategy_id"))
        object.__setattr__(
            self, "version_ref", _optional_text(self.version_ref, "version_ref")
        )
        if _SHA256.fullmatch(self.artifact_sha256) is None:
            raise LedgerValidationError("artifact_sha256 must be 64 lower-case hex")
        if self.observed_end < self.observed_start:
            raise LedgerValidationError("strategy evidence interval is reversed")


@dataclass(frozen=True)
class EvidenceComparison:
    status: VersionMatchStatus
    model_vs_actual_allowed: bool
    concatenation_allowed: bool
    reason: str


def compare_strategy_evidence(
    left: StrategyEvidenceVersion, right: StrategyEvidenceVersion
) -> EvidenceComparison:
    """Classify vintages and fail closed on live/backtest concatenation."""

    if left.strategy_id != right.strategy_id:
        return EvidenceComparison(
            VersionMatchStatus.STRATEGY_MISMATCH,
            False,
            False,
            "evidence belongs to different strategy identities",
        )
    if left.version_ref is None or right.version_ref is None:
        return EvidenceComparison(
            VersionMatchStatus.UNRESOLVED_VERSION,
            False,
            False,
            "one or both evidence vintages are unresolved",
        )
    if left.version_ref != right.version_ref:
        return EvidenceComparison(
            VersionMatchStatus.VERSION_MISMATCH,
            False,
            False,
            "evidence vintages differ",
        )
    model_layers = {
        EvidenceLayer.BACKTEST,
        EvidenceLayer.PREDECESSOR,
        EvidenceLayer.SHADOW_OOS,
    }
    live_layers = {
        EvidenceLayer.ACCOUNT_LIVE,
        EvidenceLayer.OFFICIAL_VEHICLE,
        EvidenceLayer.PROVIDER_LIVE,
    }
    layers = {left.evidence_layer, right.evidence_layer}
    model_vs_actual = bool(layers & model_layers) and bool(layers & live_layers)
    same_layer = left.evidence_layer is right.evidence_layer
    return EvidenceComparison(
        VersionMatchStatus.MATCHED,
        model_vs_actual,
        same_layer,
        (
            "same version; model-versus-actual comparison is classified"
            if model_vs_actual
            else "same version; evidence layers are not a model-versus-actual pair"
        ),
    )
