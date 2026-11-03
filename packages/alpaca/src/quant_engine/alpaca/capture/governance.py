"""Fail-closed, single-use authorization for Alpaca live reads.

Mirrors the Matrixport live-read gate.  The reviewed evidence is supplied
out-of-repository by the operator; this module validates it, creates an
exclusive consumption receipt under a caller-supplied receipt root, and records
every request reservation before the transport may touch the network.  The
evidence is never copied into a run artifact.

Differences from Matrixport, stated plainly: the receipt root is a caller
argument rather than a fixed per-user directory, and on Windows the directory
entry is made durable by file ``fsync`` plus an atomic ``os.replace`` only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import threading

from quant_engine.alpaca.capture.catalog import (
    Endpoint,
    catalog_sha256,
    endpoint_key,
    get_live_endpoint,
)
from quant_engine.alpaca.capture.config import CaptureConfig
from quant_engine.alpaca.capture.errors import CaptureRefused, GovernanceError
from quant_engine.alpaca.capture.util import canonical_json, sha256_text

TERMS_VERSION = "alpaca-live-read-terms-v1"
TERMS_TEXT = (
    "Alpaca live reads require one current, reviewed, single-use approval; "
    "GET-only approved catalog scope; an explicit paper-or-live environment; "
    "a zero-retry transport; and a durable pre-network request reservation."
)
TERMS_SHA256 = hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()
_MAX_EVIDENCE_BYTES = 64 * 1024
_MAX_REQUESTS = 10_000
_REPARSE_POINT = 0x0400
_OPERATION = "capture"
_REVIEW_REFERENCE = re.compile(
    r"^https://github\.com/dachent/quant_engine/issues/[1-9][0-9]*#issuecomment-[1-9][0-9]*$"
)
_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "operation",
        "environment",
        "account_binding",
        "scope",
        "review",
        "authority",
        "terms",
        "catalog",
        "metering",
    }
)


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GovernanceError(f"governance {name} must be an object")
    return value


def _keys(
    value: dict[str, object], name: str, expected: set[str] | frozenset[str]
) -> None:
    if set(value) != set(expected):
        raise GovernanceError(f"governance {name} has an invalid field set")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceError(f"governance {name} must be a non-empty string")
    return value


def _time(value: object, name: str) -> datetime:
    raw = _string(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise GovernanceError(
            f"governance {name} must be an RFC3339 timestamp"
        ) from None
    if parsed.tzinfo is None:
        raise GovernanceError(f"governance {name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _scope(value: object) -> frozenset[str]:
    scope = _mapping(value, "scope")
    _keys(scope, "scope", {"endpoints"})
    entries = scope["endpoints"]
    if not isinstance(entries, list) or not entries:
        raise GovernanceError("governance scope.endpoints must be a non-empty list")
    approved: set[str] = set()
    for item in entries:
        row = _mapping(item, "scope endpoint")
        _keys(row, "scope endpoint", {"method", "path"})
        method = _string(row["method"], "scope endpoint method").upper()
        path = _string(row["path"], "scope endpoint path")
        try:
            endpoint = get_live_endpoint(path)
        except CaptureRefused:
            raise GovernanceError(
                "governance scope names an unknown catalog endpoint"
            ) from None
        if method != endpoint.method:
            raise GovernanceError("governance scope names a non-live-read endpoint")
        key = endpoint_key(endpoint)
        if key in approved:
            raise GovernanceError("governance scope contains duplicate endpoints")
        approved.add(key)
    return frozenset(approved)


def _review(value: object) -> dict[str, str]:
    review = _mapping(value, "review")
    _keys(review, "review", {"identity", "reference"})
    identity = _string(review["identity"], "review identity")
    reference = _string(review["reference"], "review reference")
    if not _REVIEW_REFERENCE.fullmatch(reference):
        raise GovernanceError(
            "governance review reference must be a quant_engine issue comment URL"
        )
    return {"identity": identity, "reference": reference}


@dataclass(slots=True)
class LiveReadGovernance:
    """A consumed local operator attestation for a bounded set of calls."""

    receipt_path: Path
    evidence_sha256: str
    environment: str
    approved_endpoints: frozenset[str]
    max_requests: int
    review: dict[str, str]
    _used_requests: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def used_requests(self) -> int:
        return self._used_requests

    def reserve_request(self, endpoint: Endpoint) -> None:
        """Durably reserve one approved request before any network I/O."""

        key = endpoint_key(endpoint)
        with self._lock:
            if key not in self.approved_endpoints:
                raise GovernanceError("endpoint is not approved by live-read scope")
            if self._used_requests >= self.max_requests:
                raise GovernanceError("live-read request budget is exhausted")
            self._used_requests += 1
            receipt = {
                "schema_version": 1,
                "evidence_sha256": self.evidence_sha256,
                "operation": _OPERATION,
                "environment": self.environment,
                "review": self.review,
                "approved_endpoints": sorted(self.approved_endpoints),
                "max_requests": self.max_requests,
                "used_requests": self._used_requests,
                "retry_count": 0,
            }
            _write_receipt(
                self.receipt_path, (canonical_json(receipt) + "\n").encode("utf-8")
            )


def authorize_live_read(
    evidence_path: str | Path,
    *,
    config: CaptureConfig,
    receipt_root: str | Path,
    now: Callable[[], datetime] | None = None,
) -> LiveReadGovernance:
    """Validate and consume one operator-provided attestation artifact.

    The review reference is shape-checked but not remotely authenticated; the
    boundary assumes the trusted-workspace model documented for Matrixport.
    """

    evidence_file = Path(evidence_path)
    if not evidence_file.is_file():
        raise GovernanceError("live-read governance evidence file is required")
    try:
        evidence_bytes = evidence_file.read_bytes()
    except OSError:
        raise GovernanceError("live-read governance evidence cannot be read") from None
    if not evidence_bytes or len(evidence_bytes) > _MAX_EVIDENCE_BYTES:
        raise GovernanceError("live-read governance evidence has an invalid size")
    try:
        document = json.loads(evidence_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GovernanceError(
            "live-read governance evidence must be UTF-8 JSON"
        ) from None
    root = _mapping(document, "evidence")
    _keys(root, "evidence", _EVIDENCE_KEYS)
    if root["schema_version"] != 1:
        raise GovernanceError("unsupported live-read governance schema")
    if root["operation"] != _OPERATION:
        raise GovernanceError("governance operation is not capture")
    if root["environment"] != config.environment:
        raise GovernanceError(
            "governance environment does not match the configured environment"
        )
    binding = _mapping(root["account_binding"], "account_binding")
    _keys(binding, "account_binding", {"sha256"})
    if binding["sha256"] != sha256_text(config.account_id):
        raise GovernanceError("governance account binding does not match the config")
    approved = _scope(root["scope"])
    if approved != frozenset(config.planned_endpoints()):
        raise GovernanceError("governance scope does not equal the planned requests")
    review = _review(root["review"])

    authority = _mapping(root["authority"], "authority")
    _keys(authority, "authority", {"status", "issued_at", "expires_at"})
    if authority["status"] != "CURRENT":
        raise GovernanceError("live-read authority is not CURRENT")
    issued_at = _time(authority["issued_at"], "authority issued_at")
    expires_at = _time(authority["expires_at"], "authority expires_at")
    current = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    if issued_at > current:
        raise GovernanceError("live-read authority is not yet current")
    if expires_at <= current:
        raise GovernanceError("live-read authority is expired")

    terms = _mapping(root["terms"], "terms")
    _keys(terms, "terms", {"version", "sha256"})
    if terms["version"] != TERMS_VERSION or terms["sha256"] != TERMS_SHA256:
        raise GovernanceError("live-read governance terms do not match accepted terms")
    catalog = _mapping(root["catalog"], "catalog")
    _keys(catalog, "catalog", {"sha256"})
    if catalog["sha256"] != catalog_sha256():
        raise GovernanceError("live-read governance catalog does not match this driver")
    metering = _mapping(root["metering"], "metering")
    _keys(metering, "metering", {"max_requests", "retry_count"})
    max_requests = metering["max_requests"]
    if isinstance(max_requests, bool) or not isinstance(max_requests, int):
        raise GovernanceError("governance max_requests must be an integer")
    if not 1 <= max_requests <= _MAX_REQUESTS:
        raise GovernanceError("governance max_requests is outside the allowed range")
    if metering["retry_count"] != 0:
        raise GovernanceError("governance retry_count must be exactly zero")

    evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    receipt_dir = _prepare_receipt_root(Path(receipt_root))
    receipt_path = receipt_dir / f"{evidence_sha256}.json"
    initial = {
        "schema_version": 1,
        "evidence_sha256": evidence_sha256,
        "operation": _OPERATION,
        "environment": config.environment,
        "review": review,
        "approved_endpoints": sorted(approved),
        "max_requests": max_requests,
        "used_requests": 0,
        "retry_count": 0,
    }
    try:
        descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise GovernanceError(
            "live-read governance evidence is already consumed"
        ) from None
    except OSError:
        raise GovernanceError(
            "live-read governance receipt could not be persisted"
        ) from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write((canonical_json(initial) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        # A partial receipt is retained on purpose: reuse after an interrupted
        # consumption would be less safe than requiring a new approval.
        raise GovernanceError(
            "live-read governance receipt could not be persisted"
        ) from None
    return LiveReadGovernance(
        receipt_path=receipt_path,
        evidence_sha256=evidence_sha256,
        environment=config.environment,
        approved_endpoints=approved,
        max_requests=max_requests,
        review=review,
    )


def _write_receipt(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise GovernanceError("live-read governance receipt cannot be a symlink")
    temporary = path.with_name(f".{path.name}-{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise GovernanceError(
            "live-read governance receipt could not be durably updated"
        ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise GovernanceError("governed receipt root cannot be inspected") from None
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def _reject_reparse_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.exists() and _is_reparse(current):
            raise GovernanceError("governed receipt root cannot use a reparse ancestor")


def _prepare_receipt_root(root: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(root)))
    if not absolute.is_absolute():
        raise GovernanceError("the receipt root must be an absolute path")
    _reject_reparse_ancestors(absolute)
    try:
        absolute.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise GovernanceError("live-read receipt root cannot be created") from None
    _reject_reparse_ancestors(absolute)
    return absolute
