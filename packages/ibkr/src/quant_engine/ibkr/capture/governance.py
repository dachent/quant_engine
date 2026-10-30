"""Fail-closed, single-use authorization for IBKR Flex live reads.

Mirrors the Matrixport live-read gate: reviewed evidence is supplied out of
repository by the operator; this module validates it against the exact request
plan, creates an exclusive consumption receipt, and durably reserves every
request before the transport may touch the network.  The evidence itself is
never copied into a run artifact (only its SHA-256 and review reference are).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import threading

from ._util import canonical_json_bytes, require_keys, sha256_hex
from .catalog import catalog_sha256
from .plan import RequestPlan

OPERATION = "flex-capture"
TERMS_VERSION = "ibkr-flex-live-read-terms-v1"
TERMS_TEXT = (
    "IBKR Flex live reads require one current, reviewed, single-use approval; "
    "the exact hash-bound request plan; one SendRequest per planned request "
    "with no retry; bounded GetStatement polling for error 1019 only; and a "
    "durable pre-network request reservation."
)
TERMS_SHA256 = sha256_hex(TERMS_TEXT.encode("utf-8"))
MAX_GET_ATTEMPTS = 9
_MAX_EVIDENCE_BYTES = 64 * 1024
_REPARSE_POINT = 0x0400
_REVIEW_REFERENCE = re.compile(
    r"^https://github\.com/dachent/quant_engine/issues/[1-9][0-9]*"
    r"#issuecomment-[1-9][0-9]*$"
)


class GovernanceError(RuntimeError):
    """Live-read governance evidence is absent, invalid, or exhausted."""


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceError(f"governance {name} must be a non-empty string")
    return value


def _object(value: object, name: str, keys: set[str]) -> dict[str, object]:
    try:
        return require_keys(value, f"governance {name}", keys)
    except ValueError as exc:
        raise GovernanceError(str(exc)) from None


def _time(value: object, name: str) -> datetime:
    raw = _nonempty(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise GovernanceError(f"governance {name} must be RFC3339") from None
    if parsed.tzinfo is None:
        raise GovernanceError(f"governance {name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise GovernanceError("the receipt root cannot be inspected") from None
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _write_exclusive(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_durably(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise GovernanceError("the governance receipt cannot be a symlink")
    temporary = path.with_name(f".{path.name}-{secrets.token_hex(8)}.tmp")
    try:
        _write_exclusive(temporary, content)
        os.replace(temporary, path)
    except OSError:
        raise GovernanceError("the governance receipt could not be updated") from None
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(slots=True)
class LiveReadGovernance:
    """A consumed attestation bounding one capture."""

    receipt_path: Path
    evidence_sha256: str
    plan_sha256: str
    review: dict[str, str]
    max_send_requests: int
    max_get_attempts: int
    _sent: set[int] = field(default_factory=set)
    _gets: dict[int, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _receipt(self) -> bytes:
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "evidence_sha256": self.evidence_sha256,
                "plan_sha256": self.plan_sha256,
                "review": self.review,
                "max_send_requests": self.max_send_requests,
                "max_get_attempts": self.max_get_attempts,
                "send_requests_used": sorted(self._sent),
                "get_attempts_used": {
                    str(index): count for index, count in sorted(self._gets.items())
                },
                "retry_count": 0,
            }
        )

    def reserve_send(self, request_index: int) -> None:
        """Reserve the one SendRequest of a planned request (never repeated)."""

        with self._lock:
            if request_index in self._sent:
                raise GovernanceError("a SendRequest is never repeated")
            if len(self._sent) >= self.max_send_requests:
                raise GovernanceError("the SendRequest budget is exhausted")
            self._sent.add(request_index)
            _replace_durably(self.receipt_path, self._receipt())

    def reserve_get(self, request_index: int) -> None:
        with self._lock:
            if request_index not in self._sent:
                raise GovernanceError("GetStatement requires a reserved SendRequest")
            used = self._gets.get(request_index, 0)
            if used >= self.max_get_attempts:
                raise GovernanceError("the GetStatement budget is exhausted")
            self._gets[request_index] = used + 1
            _replace_durably(self.receipt_path, self._receipt())


def authorize_live_read(
    evidence_path: str | Path,
    *,
    plan: RequestPlan,
    receipt_root: Path,
    now: Callable[[], datetime] | None = None,
) -> LiveReadGovernance:
    """Validate and consume one operator attestation for exactly this plan."""

    evidence_file = Path(evidence_path).resolve()
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
    root = _object(
        document,
        "evidence",
        {
            "schema_version",
            "operation",
            "plan",
            "review",
            "authority",
            "terms",
            "catalog",
            "metering",
        },
    )
    if root["schema_version"] != 1:
        raise GovernanceError("unsupported live-read governance schema")
    if root["operation"] != OPERATION:
        raise GovernanceError("governance operation is not a Flex capture")
    plan_row = _object(root["plan"], "plan", {"sha256"})
    if plan_row["sha256"] != plan.sha256:
        raise GovernanceError("governance does not approve this exact request plan")
    review_row = _object(root["review"], "review", {"identity", "reference"})
    identity = _nonempty(review_row["identity"], "review identity")
    reference = _nonempty(review_row["reference"], "review reference")
    if _REVIEW_REFERENCE.fullmatch(reference) is None:
        raise GovernanceError(
            "governance review reference must be a quant_engine issue comment URL"
        )
    authority = _object(
        root["authority"], "authority", {"status", "issued_at", "expires_at"}
    )
    if authority["status"] != "CURRENT":
        raise GovernanceError("live-read authority is not CURRENT")
    issued_at = _time(authority["issued_at"], "authority issued_at")
    expires_at = _time(authority["expires_at"], "authority expires_at")
    current = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    if issued_at > current:
        raise GovernanceError("live-read authority is not yet current")
    if expires_at <= current:
        raise GovernanceError("live-read authority is expired")
    terms = _object(root["terms"], "terms", {"version", "sha256"})
    if terms["version"] != TERMS_VERSION or terms["sha256"] != TERMS_SHA256:
        raise GovernanceError("live-read governance terms do not match accepted terms")
    catalog = _object(root["catalog"], "catalog", {"sha256"})
    if catalog["sha256"] != catalog_sha256():
        raise GovernanceError("live-read governance catalog does not match this driver")
    metering = _object(
        root["metering"],
        "metering",
        {"max_send_requests", "max_get_attempts", "retry_count"},
    )
    max_send = metering["max_send_requests"]
    max_get = metering["max_get_attempts"]
    if (
        isinstance(max_send, bool)
        or not isinstance(max_send, int)
        or isinstance(max_get, bool)
        or not isinstance(max_get, int)
    ):
        raise GovernanceError("governance metering values must be integers")
    if max_send != len(plan.requests):
        raise GovernanceError("governance max_send_requests must equal the plan size")
    if not 1 <= max_get <= MAX_GET_ATTEMPTS:
        raise GovernanceError("governance max_get_attempts is outside 1..9")
    if metering["retry_count"] != 0:
        raise GovernanceError("governance retry_count must be exactly zero")

    if not receipt_root.is_dir() or _is_reparse(receipt_root):
        raise GovernanceError("the receipt root must be a plain existing directory")
    evidence_sha256 = sha256_hex(evidence_bytes)
    governance = LiveReadGovernance(
        receipt_path=receipt_root / f"{evidence_sha256}.json",
        evidence_sha256=evidence_sha256,
        plan_sha256=plan.sha256,
        review={"identity": identity, "reference": reference},
        max_send_requests=max_send,
        max_get_attempts=max_get,
    )
    try:
        _write_exclusive(governance.receipt_path, governance._receipt())
    except FileExistsError:
        raise GovernanceError(
            "live-read governance evidence is already consumed"
        ) from None
    except OSError:
        # A partial receipt is retained: reuse after an interrupted consumption
        # is less safe than requiring a new approval.
        raise GovernanceError("the governance receipt could not be persisted") from None
    return governance
