"""Provider-neutral fact authority, freshness, revision, and admission contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Final, Iterable, Mapping, Self

from quant_engine.core.contracts import (
    CoverageStatus,
    GenerationManifest,
    ManifestValidationError,
    VerificationStatus,
)


_IDENTIFIER: Final = re.compile(r"^[a-z][a-z0-9-]*$")
_GIT_COMMIT: Final = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
AUTHORITY_CATALOG_SCHEMA_VERSION: Final = 1


class EvidenceLayer(StrEnum):
    """Economic meaning of an observation; layers are never interchangeable."""

    ACCOUNT_LIVE = "ACCOUNT_LIVE"
    PROVIDER_LIVE = "PROVIDER_LIVE"
    OFFICIAL_VEHICLE = "OFFICIAL_VEHICLE"
    PREDECESSOR = "PREDECESSOR"
    SHADOW_OOS = "SHADOW_OOS"
    BACKTEST = "BACKTEST"
    STATIC_HISTORY = "STATIC_HISTORY"
    CONCEPTUAL_STRESS = "CONCEPTUAL_STRESS"


class FinalizationState(StrEnum):
    """Whether a provider may still revise an observation."""

    PROVISIONAL = "PROVISIONAL"
    FINAL = "FINAL"


class RevisionPolicy(StrEnum):
    """How source revisions are retained and interpreted."""

    IMMUTABLE_SNAPSHOT = "IMMUTABLE_SNAPSHOT"
    APPEND_ONLY = "APPEND_ONLY"
    RESTATABLE = "RESTATABLE"
    FINITE_STATIC = "FINITE_STATIC"


class RevisionState(StrEnum):
    """Whether accepted provider evidence supersedes an earlier generation."""

    ORIGINAL = "ORIGINAL"
    REVISED = "REVISED"


class PublicationDisposition(StrEnum):
    """Publication state of evidence offered to a consumer."""

    CURRENT = "CURRENT"
    PINNED_PARTIAL = "PINNED_PARTIAL"
    STAGED = "STAGED"


class FreshnessMode(StrEnum):
    """Whether a source is expected to recur or is intentionally finite."""

    RECURRING = "RECURRING"
    FINITE = "FINITE"


class FreshnessCalendar(StrEnum):
    """Elapsed-time basis used by a recurring freshness policy."""

    CALENDAR_DAY = "CALENDAR_DAY"
    WEEKDAY_SESSION = "WEEKDAY_SESSION"


class FreshnessState(StrEnum):
    """Consumer-visible freshness conclusion."""

    FRESH = "FRESH"
    LATE = "LATE"
    STALE = "STALE"
    FINITE_COMPLETE = "FINITE_COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class SubjectType(StrEnum):
    """Stable internal subject controlled by an authority rule."""

    ACCOUNT = "ACCOUNT"
    CAPITAL_POOL = "CAPITAL_POOL"
    DATASET = "DATASET"
    MARKET = "MARKET"
    PORTFOLIO = "PORTFOLIO"
    STRATEGY = "STRATEGY"


def _identifier(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ManifestValidationError(
            f"{field_name} must use lower-case portable identifier syntax"
        )
    return normalized


def _non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ManifestValidationError(f"{field_name} must not be empty")
    return normalized


def _strict_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ManifestValidationError(
            f"{name} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field_name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ManifestValidationError(f"{field_name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ManifestValidationError(f"{field_name} must use canonical ISO format")
    return parsed


def _datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ManifestValidationError(f"{field_name} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestValidationError(f"{field_name} must be timezone-aware")
    if parsed.isoformat() != value:
        raise ManifestValidationError(f"{field_name} must use canonical ISO format")
    return parsed


def _enum(enum_type: type[StrEnum], value: object, field_name: str) -> Any:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field_name} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ManifestValidationError(f"{field_name} is unknown") from exc


@dataclass(frozen=True)
class FactKey:
    """Provider, internal subject, and fact type controlled as one authority key."""

    provider_id: str
    subject_type: SubjectType
    subject_id: str
    fact_type: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _identifier(self.provider_id, "provider_id")
        )
        object.__setattr__(
            self, "subject_id", _identifier(self.subject_id, "subject_id")
        )
        object.__setattr__(self, "fact_type", _identifier(self.fact_type, "fact_type"))

    def to_dict(self) -> dict[str, str]:
        return {
            "fact_type": self.fact_type,
            "provider_id": self.provider_id,
            "subject_id": self.subject_id,
            "subject_type": self.subject_type.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict_keys(
            value,
            {"fact_type", "provider_id", "subject_id", "subject_type"},
            "fact_key",
        )
        provider_id = value["provider_id"]
        subject_id = value["subject_id"]
        fact_type = value["fact_type"]
        if not all(
            isinstance(item, str) for item in (provider_id, subject_id, fact_type)
        ):
            raise ManifestValidationError("fact key identifiers must be strings")
        return cls(
            provider_id=provider_id,
            subject_type=_enum(SubjectType, value["subject_type"], "subject_type"),
            subject_id=subject_id,
            fact_type=fact_type,
        )


@dataclass(frozen=True)
class GenerationEvidence:
    """Hash-bound operational evidence that supplements a schema-v1 manifest."""

    generation_id: str
    manifest_sha256: str
    acquired_at: datetime
    provider_revision_id: str
    adapter_commit: str
    revision_policy: RevisionPolicy
    disposition: PublicationDisposition
    supersedes_generation_id: str | None = None

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.generation_id) is None:
            raise ManifestValidationError("generation_id must be 64 lower-case hex")
        if _SHA256.fullmatch(self.manifest_sha256) is None:
            raise ManifestValidationError("manifest_sha256 must be 64 lower-case hex")
        if self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None:
            raise ManifestValidationError("acquired_at must be timezone-aware")
        object.__setattr__(
            self,
            "provider_revision_id",
            _non_empty(self.provider_revision_id, "provider_revision_id"),
        )
        if _GIT_COMMIT.fullmatch(self.adapter_commit) is None:
            raise ManifestValidationError(
                "adapter_commit must be a 40- or 64-hex commit"
            )
        if self.supersedes_generation_id is not None:
            if _SHA256.fullmatch(self.supersedes_generation_id) is None:
                raise ManifestValidationError(
                    "supersedes_generation_id must be 64 lower-case hex"
                )
            if self.supersedes_generation_id == self.generation_id:
                raise ManifestValidationError("a generation cannot supersede itself")

    def verify_manifest(self, manifest: GenerationManifest) -> None:
        """Prove the sidecar references the exact immutable manifest bytes."""

        if manifest.generation_id != self.generation_id:
            raise ManifestValidationError(
                "evidence generation_id differs from manifest"
            )
        actual = hashlib.sha256(manifest.to_json_bytes()).hexdigest()
        if actual != self.manifest_sha256:
            raise ManifestValidationError(
                "evidence manifest_sha256 differs from manifest"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "acquired_at": self.acquired_at.isoformat(),
            "adapter_commit": self.adapter_commit,
            "disposition": self.disposition.value,
            "generation_id": self.generation_id,
            "manifest_sha256": self.manifest_sha256,
            "provider_revision_id": self.provider_revision_id,
            "revision_policy": self.revision_policy.value,
            "supersedes_generation_id": self.supersedes_generation_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        expected = {
            "acquired_at",
            "adapter_commit",
            "disposition",
            "generation_id",
            "manifest_sha256",
            "provider_revision_id",
            "revision_policy",
            "supersedes_generation_id",
        }
        _strict_keys(value, expected, "generation_evidence")
        text_fields = (
            value["adapter_commit"],
            value["generation_id"],
            value["manifest_sha256"],
            value["provider_revision_id"],
        )
        if not all(isinstance(item, str) for item in text_fields):
            raise ManifestValidationError(
                "generation evidence text fields must be strings"
            )
        supersedes = value["supersedes_generation_id"]
        if supersedes is not None and not isinstance(supersedes, str):
            raise ManifestValidationError(
                "supersedes_generation_id must be a string or null"
            )
        return cls(
            generation_id=value["generation_id"],
            manifest_sha256=value["manifest_sha256"],
            acquired_at=_datetime(value["acquired_at"], "acquired_at"),
            provider_revision_id=value["provider_revision_id"],
            adapter_commit=value["adapter_commit"],
            revision_policy=_enum(
                RevisionPolicy, value["revision_policy"], "revision_policy"
            ),
            disposition=_enum(
                PublicationDisposition, value["disposition"], "disposition"
            ),
            supersedes_generation_id=supersedes,
        )


@dataclass(frozen=True)
class FactSlice:
    """Coverage and meaning of one fact interval offered by a generation."""

    key: FactKey
    effective_start: date
    effective_end: date
    evidence_layer: EvidenceLayer
    finalization_state: FinalizationState
    coverage_status: CoverageStatus
    known_gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.effective_start > self.effective_end:
            raise ManifestValidationError("fact interval is reversed")
        gaps = tuple(
            sorted({_non_empty(item, "known_gap") for item in self.known_gaps})
        )
        if (
            self.coverage_status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
            and gaps
        ):
            raise ManifestValidationError("complete fact slice cannot declare gaps")
        if (
            self.coverage_status in {CoverageStatus.PARTIAL, CoverageStatus.FAILED}
            and not gaps
        ):
            raise ManifestValidationError(
                "partial or failed fact slice must declare gaps"
            )
        object.__setattr__(self, "known_gaps", gaps)

    def to_dict(self) -> dict[str, object]:
        return {
            "coverage_status": self.coverage_status.value,
            "effective_end": self.effective_end.isoformat(),
            "effective_start": self.effective_start.isoformat(),
            "evidence_layer": self.evidence_layer.value,
            "finalization_state": self.finalization_state.value,
            "key": self.key.to_dict(),
            "known_gaps": list(self.known_gaps),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict_keys(
            value,
            {
                "coverage_status",
                "effective_end",
                "effective_start",
                "evidence_layer",
                "finalization_state",
                "key",
                "known_gaps",
            },
            "fact_slice",
        )
        key = value["key"]
        gaps = value["known_gaps"]
        if not isinstance(key, Mapping):
            raise ManifestValidationError("fact_slice.key must be an object")
        if not isinstance(gaps, list) or not all(
            isinstance(item, str) for item in gaps
        ):
            raise ManifestValidationError("fact_slice.known_gaps must be strings")
        return cls(
            key=FactKey.from_dict(key),
            effective_start=_date(value["effective_start"], "effective_start"),
            effective_end=_date(value["effective_end"], "effective_end"),
            evidence_layer=_enum(
                EvidenceLayer, value["evidence_layer"], "evidence_layer"
            ),
            finalization_state=_enum(
                FinalizationState, value["finalization_state"], "finalization_state"
            ),
            coverage_status=_enum(
                CoverageStatus, value["coverage_status"], "coverage_status"
            ),
            known_gaps=tuple(gaps),
        )


@dataclass(frozen=True)
class FreshnessPolicy:
    """Expected release cadence and staleness boundary for one fact key."""

    policy_id: str
    mode: FreshnessMode
    expected_interval_days: int | None = None
    grace_days: int = 0
    stale_after_days: int | None = None
    calendar: FreshnessCalendar = FreshnessCalendar.CALENDAR_DAY

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _identifier(self.policy_id, "policy_id"))
        if isinstance(self.grace_days, bool) or self.grace_days < 0:
            raise ManifestValidationError("grace_days must be non-negative")
        if self.mode is FreshnessMode.FINITE:
            if (
                self.expected_interval_days is not None
                or self.stale_after_days is not None
            ):
                raise ManifestValidationError(
                    "finite freshness cannot declare recurring intervals"
                )
            return
        if (
            isinstance(self.expected_interval_days, bool)
            or not isinstance(self.expected_interval_days, int)
            or self.expected_interval_days <= 0
        ):
            raise ManifestValidationError(
                "recurring freshness needs a positive interval"
            )
        if (
            isinstance(self.stale_after_days, bool)
            or not isinstance(self.stale_after_days, int)
            or self.stale_after_days < self.expected_interval_days + self.grace_days
        ):
            raise ManifestValidationError(
                "stale_after_days must cover expected interval plus grace"
            )

    def evaluate(self, observed_end: date | None, as_of_date: date) -> FreshnessState:
        if observed_end is None:
            return FreshnessState.UNAVAILABLE
        if self.mode is FreshnessMode.FINITE:
            return FreshnessState.FINITE_COMPLETE
        assert self.expected_interval_days is not None
        assert self.stale_after_days is not None
        elapsed = self.elapsed_intervals(observed_end, as_of_date)
        if elapsed <= self.expected_interval_days + self.grace_days:
            return FreshnessState.FRESH
        if elapsed <= self.stale_after_days:
            return FreshnessState.LATE
        return FreshnessState.STALE

    def elapsed_intervals(self, observed_end: date, as_of_date: date) -> int:
        """Count policy intervals after an observation through evaluation."""

        if as_of_date <= observed_end:
            return 0
        if self.calendar is FreshnessCalendar.CALENDAR_DAY:
            return (as_of_date - observed_end).days
        return sum(
            1
            for offset in range(1, (as_of_date - observed_end).days + 1)
            if (observed_end + timedelta(days=offset)).weekday() < 5
        )

    def _add_intervals(self, observed_end: date, intervals: int) -> date:
        if self.calendar is FreshnessCalendar.CALENDAR_DAY:
            return observed_end + timedelta(days=intervals)
        current = observed_end
        remaining = intervals
        while remaining:
            current += timedelta(days=1)
            if current.weekday() < 5:
                remaining -= 1
        return current

    def assess(
        self,
        observed_end: date | None,
        as_of_date: date,
        evidence: GenerationEvidence | None = None,
    ) -> FreshnessAssessment:
        state = self.evaluate(observed_end, as_of_date)
        calendar_suffix = (
            ""
            if self.calendar is FreshnessCalendar.CALENDAR_DAY
            else f" under {self.calendar.value}"
        )
        reasons = {
            FreshnessState.FRESH: (
                f"accepted observation is within cadence and grace{calendar_suffix}"
            ),
            FreshnessState.LATE: (
                f"expected refresh is late but not yet stale{calendar_suffix}"
            ),
            FreshnessState.STALE: (
                f"accepted observation exceeded the stale boundary{calendar_suffix}"
            ),
            FreshnessState.FINITE_COMPLETE: (
                "finite historical boundary is complete and has no recurring due date"
            ),
            FreshnessState.UNAVAILABLE: "no accepted observation is available",
        }
        return FreshnessAssessment(
            policy_id=self.policy_id,
            state=state,
            evaluated_on=as_of_date,
            last_accepted_date=observed_end,
            next_expected_date=self.next_expected_date(observed_end),
            acquired_at=evidence.acquired_at if evidence is not None else None,
            revision_state=(
                RevisionState.REVISED
                if evidence is not None
                and evidence.supersedes_generation_id is not None
                else RevisionState.ORIGINAL
            ),
            escalation_required=state
            in {FreshnessState.LATE, FreshnessState.STALE, FreshnessState.UNAVAILABLE},
            blocks_current_consumers=state
            in {FreshnessState.STALE, FreshnessState.UNAVAILABLE},
            reason=reasons[state],
        )

    def next_expected_date(self, observed_end: date | None) -> date | None:
        if observed_end is None or self.mode is FreshnessMode.FINITE:
            return None
        assert self.expected_interval_days is not None
        return self._add_intervals(observed_end, self.expected_interval_days)

    def to_dict(self) -> dict[str, object]:
        return {
            "calendar": self.calendar.value,
            "expected_interval_days": self.expected_interval_days,
            "grace_days": self.grace_days,
            "mode": self.mode.value,
            "policy_id": self.policy_id,
            "stale_after_days": self.stale_after_days,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        required = {
            "expected_interval_days",
            "grace_days",
            "mode",
            "policy_id",
            "stale_after_days",
        }
        actual = set(value)
        if actual != required and actual != required | {"calendar"}:
            raise ManifestValidationError("freshness_policy keys differ")
        policy_id = value["policy_id"]
        if not isinstance(policy_id, str):
            raise ManifestValidationError("freshness policy ID must be a string")
        for field in ("expected_interval_days", "grace_days", "stale_after_days"):
            item = value[field]
            if item is not None and (
                isinstance(item, bool) or not isinstance(item, int)
            ):
                raise ManifestValidationError(f"{field} must be an integer or null")
        return cls(
            policy_id=policy_id,
            mode=_enum(FreshnessMode, value["mode"], "mode"),
            expected_interval_days=value["expected_interval_days"],
            grace_days=value["grace_days"],
            stale_after_days=value["stale_after_days"],
            calendar=_enum(
                FreshnessCalendar,
                value.get("calendar", FreshnessCalendar.CALENDAR_DAY.value),
                "calendar",
            ),
        )


@dataclass(frozen=True)
class FreshnessAssessment:
    """Operator-visible, serializable result for one fact policy."""

    policy_id: str
    state: FreshnessState
    evaluated_on: date
    last_accepted_date: date | None
    next_expected_date: date | None
    acquired_at: datetime | None
    revision_state: RevisionState
    escalation_required: bool
    blocks_current_consumers: bool
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _identifier(self.policy_id, "policy_id"))
        object.__setattr__(self, "reason", _non_empty(self.reason, "reason"))
        if self.acquired_at is not None and (
            self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None
        ):
            raise ManifestValidationError(
                "freshness acquired_at must be timezone-aware"
            )
        if (
            self.last_accepted_date is not None
            and self.last_accepted_date > self.evaluated_on
        ):
            raise ManifestValidationError("freshness observation is after evaluation")
        if self.state is FreshnessState.FINITE_COMPLETE:
            if self.next_expected_date is not None:
                raise ManifestValidationError("finite freshness cannot be next-due")
            if self.blocks_current_consumers or self.escalation_required:
                raise ManifestValidationError("finite-complete history cannot escalate")
        if self.state in {FreshnessState.STALE, FreshnessState.UNAVAILABLE}:
            if not self.blocks_current_consumers or not self.escalation_required:
                raise ManifestValidationError("stale/unavailable freshness must block")

    def to_dict(self) -> dict[str, object]:
        return {
            "acquired_at": (
                self.acquired_at.isoformat() if self.acquired_at is not None else None
            ),
            "blocks_current_consumers": self.blocks_current_consumers,
            "escalation_required": self.escalation_required,
            "evaluated_on": self.evaluated_on.isoformat(),
            "last_accepted_date": (
                self.last_accepted_date.isoformat()
                if self.last_accepted_date is not None
                else None
            ),
            "next_expected_date": (
                self.next_expected_date.isoformat()
                if self.next_expected_date is not None
                else None
            ),
            "policy_id": self.policy_id,
            "reason": self.reason,
            "revision_state": self.revision_state.value,
            "state": self.state.value,
        }


@dataclass(frozen=True)
class FreshnessAlert:
    policy_id: str
    state: FreshnessState
    severity: str
    message: str


def local_freshness_alerts(
    assessments: Iterable[FreshnessAssessment],
) -> tuple[FreshnessAlert, ...]:
    """Create deterministic local alert hooks without an external side effect."""

    alerts = []
    for assessment in assessments:
        if not assessment.escalation_required:
            continue
        severity = "ERROR" if assessment.blocks_current_consumers else "WARNING"
        alerts.append(
            FreshnessAlert(
                assessment.policy_id,
                assessment.state,
                severity,
                assessment.reason,
            )
        )
    return tuple(sorted(alerts, key=lambda item: (item.severity, item.policy_id)))


@dataclass(frozen=True)
class AuthorityRule:
    """One consumer's admissible authority over an effective fact interval."""

    authority_id: str
    consumer_id: str
    key: FactKey
    effective_start: date
    effective_end: date | None
    evidence_layers: tuple[EvidenceLayer, ...]
    finalization_state: FinalizationState
    freshness_policy_id: str
    allow_partial: bool = False
    allowed_gap_codes: tuple[str, ...] = ()
    allow_non_current: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "authority_id", _identifier(self.authority_id, "authority_id")
        )
        object.__setattr__(
            self, "consumer_id", _identifier(self.consumer_id, "consumer_id")
        )
        object.__setattr__(
            self,
            "freshness_policy_id",
            _identifier(self.freshness_policy_id, "freshness_policy_id"),
        )
        layers = tuple(sorted(set(self.evidence_layers), key=lambda item: item.value))
        if not layers:
            raise ManifestValidationError("authority rule needs an evidence layer")
        if self.effective_end is not None and self.effective_start > self.effective_end:
            raise ManifestValidationError("authority interval is reversed")
        gaps = tuple(
            sorted(
                {
                    _identifier(item, "allowed_gap_code")
                    for item in self.allowed_gap_codes
                }
            )
        )
        if gaps and not self.allow_partial:
            raise ManifestValidationError("allowed gap codes require allow_partial")
        object.__setattr__(self, "evidence_layers", layers)
        object.__setattr__(self, "allowed_gap_codes", gaps)

    def covers(self, fact: FactSlice) -> bool:
        rule_end = self.effective_end or date.max
        return (
            fact.key == self.key
            and self.effective_start <= fact.effective_start
            and fact.effective_end <= rule_end
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "allow_non_current": self.allow_non_current,
            "allow_partial": self.allow_partial,
            "allowed_gap_codes": list(self.allowed_gap_codes),
            "authority_id": self.authority_id,
            "consumer_id": self.consumer_id,
            "effective_end": self.effective_end.isoformat()
            if self.effective_end
            else None,
            "effective_start": self.effective_start.isoformat(),
            "evidence_layers": [item.value for item in self.evidence_layers],
            "finalization_state": self.finalization_state.value,
            "freshness_policy_id": self.freshness_policy_id,
            "key": self.key.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict_keys(
            value,
            {
                "allow_non_current",
                "allow_partial",
                "allowed_gap_codes",
                "authority_id",
                "consumer_id",
                "effective_end",
                "effective_start",
                "evidence_layers",
                "finalization_state",
                "freshness_policy_id",
                "key",
            },
            "authority_rule",
        )
        key = value["key"]
        layers = value["evidence_layers"]
        gaps = value["allowed_gap_codes"]
        if not isinstance(key, Mapping):
            raise ManifestValidationError("authority_rule.key must be an object")
        if not isinstance(layers, list):
            raise ManifestValidationError("evidence_layers must be a list")
        if not isinstance(gaps, list) or not all(
            isinstance(item, str) for item in gaps
        ):
            raise ManifestValidationError("allowed_gap_codes must be strings")
        if not isinstance(value["allow_non_current"], bool) or not isinstance(
            value["allow_partial"], bool
        ):
            raise ManifestValidationError("authority allow flags must be booleans")
        text_fields = (
            value["authority_id"],
            value["consumer_id"],
            value["freshness_policy_id"],
        )
        if not all(isinstance(item, str) for item in text_fields):
            raise ManifestValidationError("authority identifiers must be strings")
        effective_end = value["effective_end"]
        return cls(
            authority_id=value["authority_id"],
            consumer_id=value["consumer_id"],
            key=FactKey.from_dict(key),
            effective_start=_date(value["effective_start"], "effective_start"),
            effective_end=(
                None if effective_end is None else _date(effective_end, "effective_end")
            ),
            evidence_layers=tuple(
                _enum(EvidenceLayer, item, "evidence_layer") for item in layers
            ),
            finalization_state=_enum(
                FinalizationState, value["finalization_state"], "finalization_state"
            ),
            freshness_policy_id=value["freshness_policy_id"],
            allow_partial=value["allow_partial"],
            allowed_gap_codes=tuple(gaps),
            allow_non_current=value["allow_non_current"],
        )


class AuthorityCatalog:
    """Validated rule and freshness registry with no overlapping authorities."""

    def __init__(
        self,
        rules: Iterable[AuthorityRule],
        freshness_policies: Iterable[FreshnessPolicy],
    ) -> None:
        self.rules = tuple(sorted(rules, key=lambda item: item.authority_id))
        policies = tuple(sorted(freshness_policies, key=lambda item: item.policy_id))
        if len({item.authority_id for item in self.rules}) != len(self.rules):
            raise ManifestValidationError("authority_id values must be unique")
        if len({item.policy_id for item in policies}) != len(policies):
            raise ManifestValidationError("freshness policy IDs must be unique")
        self.freshness_policies = {item.policy_id: item for item in policies}
        for rule in self.rules:
            if rule.freshness_policy_id not in self.freshness_policies:
                raise ManifestValidationError(
                    "authority references an unknown freshness policy"
                )
        self._reject_overlaps()

    def to_dict(self) -> dict[str, object]:
        return {
            "authority_catalog_schema_version": AUTHORITY_CATALOG_SCHEMA_VERSION,
            "freshness_policies": [
                self.freshness_policies[key].to_dict()
                for key in sorted(self.freshness_policies)
            ],
            "rules": [item.to_dict() for item in self.rules],
        }

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, value: bytes) -> Self:
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestValidationError(
                "authority catalog is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ManifestValidationError("authority catalog root must be an object")
        _strict_keys(
            decoded,
            {"authority_catalog_schema_version", "freshness_policies", "rules"},
            "authority_catalog",
        )
        if (
            decoded["authority_catalog_schema_version"]
            != AUTHORITY_CATALOG_SCHEMA_VERSION
        ):
            raise ManifestValidationError(
                "authority catalog schema version is unsupported"
            )
        policies = decoded["freshness_policies"]
        rules = decoded["rules"]
        if not isinstance(policies, list) or not all(
            isinstance(item, Mapping) for item in policies
        ):
            raise ManifestValidationError("freshness_policies must be objects")
        if not isinstance(rules, list) or not all(
            isinstance(item, Mapping) for item in rules
        ):
            raise ManifestValidationError("rules must be objects")
        return cls(
            rules=(AuthorityRule.from_dict(item) for item in rules),
            freshness_policies=(FreshnessPolicy.from_dict(item) for item in policies),
        )

    def _reject_overlaps(self) -> None:
        for index, left in enumerate(self.rules):
            for right in self.rules[index + 1 :]:
                if left.consumer_id != right.consumer_id or left.key != right.key:
                    continue
                left_end = left.effective_end or date.max
                right_end = right.effective_end or date.max
                if max(left.effective_start, right.effective_start) <= min(
                    left_end, right_end
                ):
                    raise ManifestValidationError(
                        "overlapping authority rules for the same consumer and fact key"
                    )

    def authority_for(self, consumer_id: str, fact: FactSlice) -> AuthorityRule:
        consumer = _identifier(consumer_id, "consumer_id")
        matches = [
            rule
            for rule in self.rules
            if rule.consumer_id == consumer and rule.covers(fact)
        ]
        if len(matches) != 1:
            raise ManifestValidationError(
                f"expected exactly one authority rule; found {len(matches)}"
            )
        return matches[0]


@dataclass(frozen=True)
class AdmissionDecision:
    """Deterministic consumer decision with explicit reasons and freshness."""

    admitted: bool
    authority_id: str | None
    freshness_state: FreshnessState
    reasons: tuple[str, ...]
    next_expected_date: date | None


def decide_admission(
    *,
    manifest: GenerationManifest,
    evidence: GenerationEvidence,
    fact: FactSlice,
    catalog: AuthorityCatalog,
    consumer_id: str,
    evaluation_date: date,
) -> AdmissionDecision:
    """Admit a fact only when integrity, authority, coverage, and freshness pass."""

    evidence.verify_manifest(manifest)
    rule = catalog.authority_for(consumer_id, fact)
    policy = catalog.freshness_policies[rule.freshness_policy_id]
    freshness = policy.evaluate(fact.effective_end, evaluation_date)
    reasons: list[str] = []

    if manifest.verification is not VerificationStatus.VERIFIED:
        reasons.append("GENERATION_NOT_VERIFIED")
    if fact.evidence_layer not in rule.evidence_layers:
        reasons.append("EVIDENCE_LAYER_NOT_AUTHORIZED")
    if fact.finalization_state is not rule.finalization_state:
        reasons.append("FINALIZATION_STATE_MISMATCH")
    if fact.coverage_status is CoverageStatus.FAILED:
        reasons.append("FACT_COVERAGE_FAILED")
    elif fact.coverage_status is CoverageStatus.PARTIAL:
        if not rule.allow_partial:
            reasons.append("PARTIAL_FACT_NOT_AUTHORIZED")
        elif not set(fact.known_gaps).issubset(rule.allowed_gap_codes):
            reasons.append("PARTIAL_FACT_HAS_UNAUTHORIZED_GAPS")
    if (
        evidence.disposition is not PublicationDisposition.CURRENT
        and not rule.allow_non_current
    ):
        reasons.append("GENERATION_NOT_CURRENT")
    if freshness in {FreshnessState.STALE, FreshnessState.UNAVAILABLE}:
        reasons.append(f"FRESHNESS_{freshness.value}")

    return AdmissionDecision(
        admitted=not reasons,
        authority_id=rule.authority_id,
        freshness_state=freshness,
        reasons=tuple(reasons),
        next_expected_date=policy.next_expected_date(fact.effective_end),
    )
