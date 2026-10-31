"""Assemble and check the single-use live-read governance evidence file.

The generator only *assembles* the file from values the caller supplies (review
issue-comment URL and identity, authority window, request ceiling, cutoff) plus
values it computes (terms and catalog hashes, the exact catalog scope for the
chosen operation, the credential-source identity).  It never invents or fetches a
review reference and never approves anything: the reviewed authority must already
exist in the governing GitHub issue.

Neither the evidence nor the provenance sidecar holds a credential value or a
secret name; ``credential_identity`` is the digest of the ordered plan references.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import governance
from .catalog import ENDPOINTS
from .credentials import plan_identity
from .governance import (
    TERMS_SHA256,
    TERMS_VERSION,
    GovernanceError,
    ValidatedEvidence,
    catalog_sha256,
    validate_live_read_evidence,
)
from .util import canonical_json

DEFAULT_MAX_WINDOW_HOURS = 24.0
_AUTH_TEST_PATH = "/mapi/v1/wallet/balance"
_REVIEW_URL_RE = re.compile(
    r"^https://github\.com/dachent/quant_engine/issues/[1-9][0-9]*#issuecomment-[1-9][0-9]*$"
)
_OPERATIONS = ("auth-test", "extract", "run")


class EvidenceGenerationError(RuntimeError):
    """The evidence cannot be assembled or does not check out; nothing was written."""


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    operation: str
    mode: str | None
    cutoff: str
    review_url: str
    review_identity: str
    issued_at: str
    expires_at: str
    max_requests: int
    max_window_hours: float = DEFAULT_MAX_WINDOW_HOURS
    scope_paths: tuple[str, ...] | None = None
    credentials_config: Path | None = None


def _require_mode(operation: str, mode: str | None) -> str | None:
    if operation not in _OPERATIONS:
        raise EvidenceGenerationError("operation must be auth-test, extract or run")
    if operation == "auth-test":
        if mode is not None:
            raise EvidenceGenerationError("auth-test takes no mode")
        return None
    if operation == "run":
        if mode not in (None, "full"):
            raise EvidenceGenerationError("run mode must be full")
        return "full"
    if mode not in {"full", "incremental"}:
        raise EvidenceGenerationError("extract mode must be full or incremental")
    return mode


def planned_scope(operation: str) -> list[str]:
    """The exact catalog reads an operation may make, as sorted `GET path` keys."""
    if operation == "auth-test":
        return [f"GET {_AUTH_TEST_PATH}"]
    return sorted(
        f"{e.method} {e.path}"
        for e in ENDPOINTS
        if e.live_enabled and e.safety_class == "read_only"
    )


def _parse_instant(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EvidenceGenerationError(f"{name} must be an RFC3339 timestamp") from None
    if parsed.tzinfo is None:
        raise EvidenceGenerationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_roots(path: Path) -> list[Path]:
    return [p for p in (path, *path.parents) if (p / ".git").exists()]


def _check_output_path(out: Path) -> Path:
    if not out.is_absolute():
        raise EvidenceGenerationError("output path must be absolute")
    # resolve symlinks in the directory so the repository exclusion sees the real location
    resolved = Path(os.path.abspath(out))
    resolved = resolved.parent.resolve() / resolved.name
    if resolved.suffix != ".json":
        raise EvidenceGenerationError("output path must end in .json")
    for root in _repo_roots(Path(__file__).resolve().parent):
        if resolved == root or root in resolved.parents:
            raise EvidenceGenerationError("output path must be outside the repository")
    if _repo_roots(resolved.parent):
        raise EvidenceGenerationError("output path must be outside any repository")
    if not resolved.parent.is_dir():
        raise EvidenceGenerationError("output directory must already exist")
    for target in (resolved, _sidecar_path(resolved)):
        if target.exists() or target.is_symlink():
            raise EvidenceGenerationError("output file already exists (create-only)")
    return resolved


def _sidecar_path(out: Path) -> Path:
    return out.with_name(out.name[: -len(".json")] + ".provenance.json")


def _create_only(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _check_window(
    issued: datetime, expires: datetime, current: datetime, max_hours: float
) -> None:
    if max_hours <= 0:
        raise EvidenceGenerationError("maximum window must be positive")
    if expires <= issued:
        raise EvidenceGenerationError("authority expires_at must be after issued_at")
    if expires <= current:
        raise EvidenceGenerationError("authority is expired")
    if issued > current:
        raise EvidenceGenerationError("authority issued_at is in the future")
    if expires - issued > timedelta(hours=max_hours):
        raise EvidenceGenerationError("authority window exceeds the declared maximum")


def _scope_keys(paths: Iterable[str]) -> list[str]:
    return sorted(f"GET {p}" for p in paths)


def build_evidence(
    request: EvidenceRequest, *, now: Callable[[], datetime] | None = None
) -> tuple[dict[str, object], dict[str, object]]:
    """Return ``(evidence, provenance)`` documents or raise ``EvidenceGenerationError``."""
    current = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    mode = _require_mode(request.operation, request.mode)
    if not isinstance(request.review_url, str) or not _REVIEW_URL_RE.fullmatch(
        request.review_url
    ):
        raise EvidenceGenerationError(
            "review URL must be https://github.com/dachent/quant_engine/issues/<n>#issuecomment-<id>"
        )
    if not request.review_identity.strip():
        raise EvidenceGenerationError("review identity is required")
    try:
        cutoff = date.fromisoformat(request.cutoff)
    except ValueError:
        raise EvidenceGenerationError("cutoff must be YYYY-MM-DD") from None
    if cutoff > current.date():
        raise EvidenceGenerationError("cutoff is in the future")
    issued = _parse_instant(request.issued_at, "issued_at")
    expires = _parse_instant(request.expires_at, "expires_at")
    _check_window(issued, expires, current, request.max_window_hours)

    expected = planned_scope(request.operation)
    if request.scope_paths is not None and _scope_keys(request.scope_paths) != expected:
        raise EvidenceGenerationError(
            "planned scope differs from the catalog reads for this operation"
        )
    if isinstance(request.max_requests, bool) or not isinstance(
        request.max_requests, int
    ):
        raise EvidenceGenerationError("max requests must be an integer")
    if not 1 <= request.max_requests <= 10_000:
        raise EvidenceGenerationError("max requests must be between 1 and 10000")
    if request.operation == "auth-test" and request.max_requests != 1:
        raise EvidenceGenerationError("auth-test makes exactly one request")
    if request.max_requests < len(expected):
        raise EvidenceGenerationError("max requests is below one call per endpoint")

    evidence: dict[str, object] = {
        "schema_version": 1,
        "operation": request.operation,
        "mode": mode,
        "scope": {
            "endpoints": [
                {"method": key.split(" ", 1)[0], "path": key.split(" ", 1)[1]}
                for key in expected
            ]
        },
        "review": {
            "identity": request.review_identity.strip(),
            "reference": request.review_url,
        },
        "authority": {
            "status": "CURRENT",
            "issued_at": _utc_text(issued),
            "expires_at": _utc_text(expires),
        },
        "terms": {"version": TERMS_VERSION, "sha256": TERMS_SHA256},
        "catalog": {"sha256": catalog_sha256()},
        "metering": {"max_requests": request.max_requests, "retry_count": 0},
    }
    provenance: dict[str, object] = {
        "schema_version": 1,
        "kind": "matrixport-governance-evidence-provenance",
        "cutoff": request.cutoff,
        "operation": request.operation,
        "mode": mode,
        "generated_at": _utc_text(current),
        "max_window_hours": request.max_window_hours,
        "credential_identity": (
            plan_identity(request.credentials_config)
            if request.credentials_config is not None
            else None
        ),
        "note": (
            "cutoff and credential_identity are not fields of the gate's evidence "
            "schema (its field set is exact), so they are recorded here instead"
        ),
    }
    return evidence, provenance


def write_evidence(
    request: EvidenceRequest,
    out: Path,
    *,
    now: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Assemble the evidence, check it with the gate's validator, write it create-only."""
    target = _check_output_path(out)
    mode = _require_mode(request.operation, request.mode)
    evidence, provenance = build_evidence(request, now=now)
    data = (canonical_json(evidence) + "\n").encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    provenance["evidence_sha256"] = digest
    sidecar = _sidecar_path(target)
    _create_only(target, data)
    try:
        # the validator reads the bytes just written; a failure leaves nothing behind
        validate_live_read_evidence(
            target,
            operation=request.operation,
            mode=mode,
            now=now,
        )
    except GovernanceError as error:
        target.unlink(missing_ok=True)
        raise EvidenceGenerationError(f"gate validator rejected the file: {error}")
    try:
        _create_only(sidecar, (canonical_json(provenance) + "\n").encode("utf-8"))
    except OSError:
        # never leave a consumable evidence file without its provenance
        target.unlink(missing_ok=True)
        raise EvidenceGenerationError(
            "provenance sidecar could not be created"
        ) from None
    return {
        "ok": True,
        "evidence": str(target),
        "provenance": str(sidecar),
        "evidence_sha256": digest,
        "operation": request.operation,
        "mode": evidence["mode"],
        "endpoint_count": len(planned_scope(request.operation)),
        "max_requests": request.max_requests,
        "expires_at": evidence["authority"]["expires_at"],  # type: ignore[index]
    }


def verify_evidence(
    path: Path,
    *,
    max_window_hours: float = DEFAULT_MAX_WINDOW_HOURS,
    now: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    """Re-derive hashes and scope, run the gate validator; never consume the file."""
    try:
        raw = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise EvidenceGenerationError(
            "evidence file is not readable UTF-8 JSON"
        ) from None
    if not isinstance(raw, dict):
        raise EvidenceGenerationError("evidence must be a JSON object")
    operation, mode = raw.get("operation"), raw.get("mode")
    if not isinstance(operation, str):
        raise EvidenceGenerationError("evidence operation is missing")
    try:
        validated: ValidatedEvidence = validate_live_read_evidence(
            path, operation=operation, mode=mode, now=now
        )
    except GovernanceError as error:
        raise EvidenceGenerationError(f"gate validator rejected the file: {error}")
    # re-derive independently of the validator's own comparisons
    terms, catalog = raw["terms"], raw["catalog"]
    if terms.get("sha256") != TERMS_SHA256 or catalog.get("sha256") != catalog_sha256():
        raise EvidenceGenerationError("terms or catalog hash differs from the checkout")
    if sorted(validated.approved_endpoints) != planned_scope(operation):
        raise EvidenceGenerationError("scope is not exactly the catalog reads")
    if validated.expires_at - validated.issued_at > timedelta(hours=max_window_hours):
        raise EvidenceGenerationError("authority window exceeds the declared maximum")
    consumed = False
    try:
        consumed = (
            governance._canonical_state_root() / f"{validated.evidence_sha256}.json"
        ).exists()
    except GovernanceError:
        consumed = False
    if consumed:
        raise EvidenceGenerationError("evidence is already consumed")
    return {
        "ok": True,
        "consumed": False,
        "evidence_sha256": validated.evidence_sha256,
        "operation": operation,
        "mode": mode,
        "endpoint_count": len(validated.approved_endpoints),
        "max_requests": validated.max_requests,
        "review_reference": validated.review["reference"],
        "expires_at": _utc_text(validated.expires_at),
    }


_DAY_MS = 86_400_000


def cutoff_end_ms(cutoff: str, now_ms: int) -> int:
    """The last millisecond of the cutoff day (UTC), capped at the clock.

    Every windowed request of a run ends here, so no request window extends past
    the cutoff day. A cutoff in the future is refused.
    """
    try:
        day = date.fromisoformat(cutoff)
    except (TypeError, ValueError):
        raise EvidenceGenerationError("cutoff must be YYYY-MM-DD") from None
    start = (
        int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        * 1000
    )
    if start > now_ms:
        raise EvidenceGenerationError("cutoff is in the future")
    return min(start + _DAY_MS - 1, now_ms)


def bound_cutoff(
    evidence_path: str | Path,
    cutoff: str | None,
    *,
    operation: str,
    mode: str | None,
    now_ms: int | None = None,
) -> str:
    """Return ``cutoff`` once it equals the one the evidence was generated for.

    Reads the provenance sidecar next to the evidence file, requires it to be
    bound to these exact evidence bytes, operation and mode, and refuses a
    missing, malformed or different cutoff. Reads only; consumes nothing.
    """
    if not cutoff:
        raise EvidenceGenerationError("a live run requires --cutoff")
    evidence = Path(evidence_path)
    sidecar = _sidecar_path(evidence)
    try:
        data = evidence.read_bytes()
        record = json.loads(sidecar.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise EvidenceGenerationError(
            "evidence provenance sidecar is missing or unreadable"
        ) from None
    if (
        not isinstance(record, dict)
        or record.get("kind") != "matrixport-governance-evidence-provenance"
        or record.get("evidence_sha256") != hashlib.sha256(data).hexdigest()
        or record.get("operation") != operation
        or record.get("mode") != mode
    ):
        raise EvidenceGenerationError(
            "provenance sidecar is not bound to this evidence file"
        )
    if record.get("cutoff") != cutoff:
        raise EvidenceGenerationError("cutoff differs from the evidence provenance")
    cutoff_end_ms(
        cutoff,
        now_ms
        if now_ms is not None
        else int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    return cutoff
