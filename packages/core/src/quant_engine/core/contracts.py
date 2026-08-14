"""Deterministic, provider-neutral generation manifest contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Final, Mapping, Self


MANIFEST_SCHEMA_VERSION: Final = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManifestValidationError(ValueError):
    """The manifest does not satisfy the versioned core contract."""


class CoverageStatus(StrEnum):
    """Coverage result inside the claim's explicitly declared boundary."""

    COMPLETE_WITHIN_DECLARED_BOUNDARY = "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class VerificationStatus(StrEnum):
    """Verification state of an immutable generation."""

    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


def _non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ManifestValidationError(f"{field_name} must not be empty")
    return normalized


def _strict_keys(
    value: Mapping[str, Any], expected: set[str], object_name: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise ManifestValidationError(
            f"{object_name} keys differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def _parse_date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{field_name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ManifestValidationError(f"{field_name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ManifestValidationError(f"{field_name} must use canonical ISO format")
    return parsed


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize a manifest value using the sole canonical JSON encoding."""

    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def hash_file(path: Path, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """Return a lower-case SHA-256 and byte count without retaining file content."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class AdapterIdentity:
    """Name and version of the code that produced normalized evidence."""

    name: str
    version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _non_empty(self.name, "adapter.name"))
        object.__setattr__(self, "version", _non_empty(self.version, "adapter.version"))

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict_keys(value, {"name", "version"}, "adapter")
        if not isinstance(value["name"], str) or not isinstance(value["version"], str):
            raise ManifestValidationError("adapter name and version must be strings")
        return cls(name=value["name"], version=value["version"])


@dataclass(frozen=True)
class SourceArtifact:
    """Hash-bound source input without a workstation path or private content."""

    artifact_id: str
    sha256: str
    size_bytes: int
    as_of_date: date
    schema_name: str
    schema_version: str

    def __post_init__(self) -> None:
        artifact_id = _non_empty(self.artifact_id, "artifact_id")
        portable = PurePosixPath(artifact_id)
        if (
            portable.is_absolute()
            or "\\" in artifact_id
            or any(part in {"", ".", ".."} for part in portable.parts)
        ):
            raise ManifestValidationError(
                "artifact_id must be a portable relative evidence identifier"
            )
        if _SHA256.fullmatch(self.sha256) is None:
            raise ManifestValidationError("artifact sha256 must be 64 lower-case hex")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ManifestValidationError("artifact size_bytes must be non-negative")
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(
            self, "schema_name", _non_empty(self.schema_name, "schema_name")
        )
        object.__setattr__(
            self, "schema_version", _non_empty(self.schema_version, "schema_version")
        )

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        artifact_id: str,
        as_of_date: date,
        schema_name: str,
        schema_version: str,
    ) -> Self:
        sha256, size_bytes = hash_file(path)
        return cls(
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            as_of_date=as_of_date,
            schema_name=schema_name,
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "as_of_date": self.as_of_date.isoformat(),
            "schema": {"name": self.schema_name, "version": self.schema_version},
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict_keys(
            value,
            {"artifact_id", "as_of_date", "schema", "sha256", "size_bytes"},
            "artifact",
        )
        schema = value["schema"]
        if not isinstance(schema, Mapping):
            raise ManifestValidationError("artifact schema must be an object")
        _strict_keys(schema, {"name", "version"}, "artifact.schema")
        artifact_id = value["artifact_id"]
        sha256 = value["sha256"]
        size_bytes = value["size_bytes"]
        schema_name = schema["name"]
        schema_version = schema["version"]
        if not all(
            isinstance(item, str)
            for item in (artifact_id, sha256, schema_name, schema_version)
        ):
            raise ManifestValidationError("artifact text fields must be strings")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise ManifestValidationError("artifact size_bytes must be an integer")
        return cls(
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            as_of_date=_parse_date(value["as_of_date"], "artifact.as_of_date"),
            schema_name=schema_name,
            schema_version=schema_version,
        )


@dataclass(frozen=True)
class DeclaredExclusion:
    """One owner-accepted exclusion that narrows the coverage boundary.

    Each exclusion records the exact item or gap identifier that the owner has
    declared out of scope, the rationale, and the provenance of the declaration
    (e.g. an issue reference and date).
    """

    item: str
    reason: str
    provenance: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "item", _non_empty(self.item, "exclusion.item"))
        object.__setattr__(self, "reason", _non_empty(self.reason, "exclusion.reason"))
        object.__setattr__(
            self, "provenance", _non_empty(self.provenance, "exclusion.provenance")
        )

    def to_dict(self) -> dict[str, str]:
        return {"item": self.item, "reason": self.reason, "provenance": self.provenance}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        _strict_keys(value, {"item", "reason", "provenance"}, "declared_exclusion")
        if not all(
            isinstance(value.get(k), str) for k in ("item", "reason", "provenance")
        ):
            raise ManifestValidationError("declared_exclusion fields must be strings")
        return cls(
            item=value["item"],
            reason=value["reason"],
            provenance=value["provenance"],
        )


@dataclass(frozen=True)
class CoverageClaim:
    """Coverage conclusion and its exact dataset/fact/time boundary."""

    status: CoverageStatus
    boundary: str
    fact_types: tuple[str, ...]
    observed_start: date | None = None
    observed_end: date | None = None
    known_gaps: tuple[str, ...] = ()
    declared_exclusions: tuple[DeclaredExclusion, ...] = ()
    undeclared_gaps: tuple[str, ...] = ()
    stale_declarations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        boundary = _non_empty(self.boundary, "coverage.boundary")
        facts = tuple(
            sorted({_non_empty(item, "coverage.fact_type") for item in self.fact_types})
        )
        gaps = tuple(
            sorted({_non_empty(item, "coverage.known_gap") for item in self.known_gaps})
        )
        exclusions = tuple(sorted(self.declared_exclusions, key=lambda e: e.item))
        undeclared = tuple(
            sorted(
                {
                    _non_empty(item, "coverage.undeclared_gap")
                    for item in self.undeclared_gaps
                }
            )
        )
        stale = tuple(
            sorted(
                {
                    _non_empty(item, "coverage.stale_declaration")
                    for item in self.stale_declarations
                }
            )
        )
        if not facts:
            raise ManifestValidationError("coverage.fact_types must not be empty")
        if (self.observed_start is None) != (self.observed_end is None):
            raise ManifestValidationError(
                "coverage observed_start and observed_end must both be set or absent"
            )
        if (
            self.observed_start is not None
            and self.observed_end is not None
            and self.observed_start > self.observed_end
        ):
            raise ManifestValidationError("coverage interval is reversed")
        if self.status is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY and gaps:
            raise ManifestValidationError(
                "complete coverage cannot declare gaps inside its boundary"
            )
        if self.status in {CoverageStatus.PARTIAL, CoverageStatus.FAILED} and not gaps:
            raise ManifestValidationError(
                "partial or failed coverage must declare at least one gap"
            )
        object.__setattr__(self, "boundary", boundary)
        object.__setattr__(self, "fact_types", facts)
        object.__setattr__(self, "known_gaps", gaps)
        object.__setattr__(self, "declared_exclusions", exclusions)
        object.__setattr__(self, "undeclared_gaps", undeclared)
        object.__setattr__(self, "stale_declarations", stale)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "boundary": self.boundary,
            "fact_types": list(self.fact_types),
            "known_gaps": list(self.known_gaps),
            "observed_end": (
                self.observed_end.isoformat() if self.observed_end is not None else None
            ),
            "observed_start": (
                self.observed_start.isoformat()
                if self.observed_start is not None
                else None
            ),
            "status": self.status.value,
        }
        if self.declared_exclusions:
            result["declared_exclusions"] = [
                exclusion.to_dict() for exclusion in self.declared_exclusions
            ]
        if self.undeclared_gaps:
            result["undeclared_gaps"] = list(self.undeclared_gaps)
        if self.stale_declarations:
            result["stale_declarations"] = list(self.stale_declarations)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        required_keys = {
            "boundary",
            "fact_types",
            "known_gaps",
            "observed_end",
            "observed_start",
            "status",
        }
        allowed_optional = {
            "declared_exclusions",
            "undeclared_gaps",
            "stale_declarations",
        }
        extra = set(value) - required_keys - allowed_optional
        if extra:
            raise ManifestValidationError(
                f"coverage contains unexpected keys: {sorted(extra)}"
            )
        missing = required_keys - set(value)
        if missing:
            raise ManifestValidationError(
                f"coverage is missing required keys: {sorted(missing)}"
            )
        boundary = value["boundary"]
        fact_types = value["fact_types"]
        known_gaps = value["known_gaps"]
        status = value["status"]
        if not isinstance(boundary, str) or not isinstance(status, str):
            raise ManifestValidationError(
                "coverage boundary and status must be strings"
            )
        if not isinstance(fact_types, list) or not all(
            isinstance(item, str) for item in fact_types
        ):
            raise ManifestValidationError("coverage fact_types must be strings")
        if not isinstance(known_gaps, list) or not all(
            isinstance(item, str) for item in known_gaps
        ):
            raise ManifestValidationError("coverage known_gaps must be strings")
        try:
            parsed_status = CoverageStatus(status)
        except ValueError as exc:
            raise ManifestValidationError("coverage status is unknown") from exc
        observed_start = value["observed_start"]
        observed_end = value["observed_end"]
        raw_exclusions = value.get("declared_exclusions", [])
        if not isinstance(raw_exclusions, list) or not all(
            isinstance(item, Mapping) for item in raw_exclusions
        ):
            raise ManifestValidationError(
                "coverage declared_exclusions must be objects"
            )
        exclusions = tuple(DeclaredExclusion.from_dict(item) for item in raw_exclusions)

        def _parse_string_list(raw: object, label: str) -> tuple[str, ...]:
            if not isinstance(raw, list) or not all(
                isinstance(item, str) for item in raw
            ):
                raise ManifestValidationError(f"coverage {label} must be strings")
            return tuple(raw)

        undeclared = _parse_string_list(
            value.get("undeclared_gaps", []), "undeclared_gaps"
        )
        stale = _parse_string_list(
            value.get("stale_declarations", []), "stale_declarations"
        )
        return cls(
            status=parsed_status,
            boundary=boundary,
            fact_types=tuple(fact_types),
            observed_start=(
                None
                if observed_start is None
                else _parse_date(observed_start, "coverage.observed_start")
            ),
            observed_end=(
                None
                if observed_end is None
                else _parse_date(observed_end, "coverage.observed_end")
            ),
            known_gaps=tuple(known_gaps),
            declared_exclusions=exclusions,
            undeclared_gaps=undeclared,
            stale_declarations=stale,
        )


@dataclass(frozen=True)
class GenerationManifest:
    """Immutable, content-addressed description of one normalized generation."""

    generation_id: str
    as_of_date: date
    adapter: AdapterIdentity
    coverage: CoverageClaim
    artifacts: tuple[SourceArtifact, ...]
    verification: VerificationStatus
    manifest_schema_version: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.manifest_schema_version != MANIFEST_SCHEMA_VERSION:
            raise ManifestValidationError("manifest schema version is unsupported")
        artifacts = tuple(sorted(self.artifacts, key=lambda item: item.artifact_id))
        if not artifacts:
            raise ManifestValidationError(
                "generation must contain at least one artifact"
            )
        identifiers = [item.artifact_id for item in artifacts]
        if len(identifiers) != len(set(identifiers)):
            raise ManifestValidationError("artifact_id values must be unique")
        if self.as_of_date < max(item.as_of_date for item in artifacts):
            raise ManifestValidationError(
                "generation as_of_date cannot precede an artifact as_of_date"
            )
        object.__setattr__(self, "artifacts", artifacts)
        expected = hashlib.sha256(
            canonical_json_bytes(self.identity_payload())
        ).hexdigest()
        if self.generation_id != expected:
            raise ManifestValidationError(
                "generation_id does not match manifest content"
            )

    @classmethod
    def build(
        cls,
        *,
        as_of_date: date,
        adapter: AdapterIdentity,
        coverage: CoverageClaim,
        artifacts: tuple[SourceArtifact, ...],
        verification: VerificationStatus,
    ) -> Self:
        ordered = tuple(sorted(artifacts, key=lambda item: item.artifact_id))
        payload = cls._payload_from_values(
            as_of_date=as_of_date,
            adapter=adapter,
            coverage=coverage,
            artifacts=ordered,
            verification=verification,
        )
        generation_id = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        return cls(
            generation_id=generation_id,
            as_of_date=as_of_date,
            adapter=adapter,
            coverage=coverage,
            artifacts=ordered,
            verification=verification,
        )

    @staticmethod
    def _payload_from_values(
        *,
        as_of_date: date,
        adapter: AdapterIdentity,
        coverage: CoverageClaim,
        artifacts: tuple[SourceArtifact, ...],
        verification: VerificationStatus,
    ) -> dict[str, object]:
        return {
            "adapter": adapter.to_dict(),
            "artifacts": [artifact.to_dict() for artifact in artifacts],
            "as_of_date": as_of_date.isoformat(),
            "coverage": coverage.to_dict(),
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "verification": verification.value,
        }

    def identity_payload(self) -> dict[str, object]:
        return self._payload_from_values(
            as_of_date=self.as_of_date,
            adapter=self.adapter,
            coverage=self.coverage,
            artifacts=self.artifacts,
            verification=self.verification,
        )

    def to_dict(self) -> dict[str, object]:
        return {"generation_id": self.generation_id, **self.identity_payload()}

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_json_bytes(cls, value: bytes) -> Self:
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestValidationError("manifest is not valid UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise ManifestValidationError("manifest root must be an object")
        _strict_keys(
            decoded,
            {
                "adapter",
                "artifacts",
                "as_of_date",
                "coverage",
                "generation_id",
                "manifest_schema_version",
                "verification",
            },
            "manifest",
        )
        adapter = decoded["adapter"]
        artifacts = decoded["artifacts"]
        coverage = decoded["coverage"]
        generation_id = decoded["generation_id"]
        schema_version = decoded["manifest_schema_version"]
        verification = decoded["verification"]
        if not isinstance(adapter, Mapping) or not isinstance(coverage, Mapping):
            raise ManifestValidationError("adapter and coverage must be objects")
        if not isinstance(artifacts, list) or not all(
            isinstance(item, Mapping) for item in artifacts
        ):
            raise ManifestValidationError("artifacts must be objects")
        if not isinstance(generation_id, str):
            raise ManifestValidationError("generation_id must be a string")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ManifestValidationError("manifest_schema_version must be an integer")
        if not isinstance(verification, str):
            raise ManifestValidationError("verification must be a string")
        try:
            parsed_verification = VerificationStatus(verification)
        except ValueError as exc:
            raise ManifestValidationError("verification status is unknown") from exc
        return cls(
            generation_id=generation_id,
            as_of_date=_parse_date(decoded["as_of_date"], "manifest.as_of_date"),
            adapter=AdapterIdentity.from_dict(adapter),
            coverage=CoverageClaim.from_dict(coverage),
            artifacts=tuple(SourceArtifact.from_dict(item) for item in artifacts),
            verification=parsed_verification,
            manifest_schema_version=schema_version,
        )
