"""Fail-closed, single-use authorization for Matrixport live reads.

The reviewed evidence is supplied out-of-repository by the operator.  This
module validates it, creates an exclusive local consumption receipt, and
records each request reservation before the transport is permitted to call the
network.  Evidence is intentionally never copied into a run artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .catalog import Endpoint, catalog_rows, get_endpoint
from .util import canonical_json


TERMS_VERSION = "matrixport-live-read-terms-v1"
TERMS_TEXT = (
    "Matrixport live reads require one current, reviewed, single-use approval; "
    "GET-only approved catalog scope; a zero-retry transport; and a durable "
    "pre-network request reservation."
)
TERMS_SHA256 = hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()
_MAX_EVIDENCE_BYTES = 64 * 1024
_MAX_REQUESTS = 10_000
_REPARSE_POINT = 0x0400
_REVIEW_REFERENCE_RE = re.compile(
    r"^https://github\.com/dachent/quant_engine/issues/[1-9][0-9]*#issuecomment-[1-9][0-9]*$"
)


class GovernanceError(RuntimeError):
    """Live-read governance evidence is absent, invalid, or exhausted."""


def catalog_sha256() -> str:
    """Return the deterministic identity of the checked endpoint catalog."""
    return hashlib.sha256(canonical_json(catalog_rows()).encode("utf-8")).hexdigest()


def _as_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise GovernanceError(f"governance {name} must be an object")
    return value


def _require_keys(value: dict[str, object], name: str, expected: set[str]) -> None:
    if set(value) != expected:
        raise GovernanceError(f"governance {name} has an invalid field set")


def _as_nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceError(f"governance {name} must be a non-empty string")
    return value


def _parse_time(value: object, name: str) -> datetime:
    raw = _as_nonempty_string(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise GovernanceError(
            f"governance {name} must be an RFC3339 timestamp"
        ) from None
    if parsed.tzinfo is None:
        raise GovernanceError(f"governance {name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _endpoint_key(endpoint: Endpoint) -> str:
    return f"{endpoint.method} {endpoint.path}"


def _parse_scope(value: object) -> frozenset[str]:
    scope = _as_mapping(value, "scope")
    _require_keys(scope, "scope", {"endpoints"})
    raw_endpoints = scope["endpoints"]
    if not isinstance(raw_endpoints, list) or not raw_endpoints:
        raise GovernanceError("governance scope.endpoints must be a non-empty list")
    approved: set[str] = set()
    for item in raw_endpoints:
        row = _as_mapping(item, "scope endpoint")
        _require_keys(row, "scope endpoint", {"method", "path"})
        method = _as_nonempty_string(row["method"], "scope endpoint method").upper()
        path = _as_nonempty_string(row["path"], "scope endpoint path")
        try:
            endpoint = get_endpoint(path, method=method)
        except KeyError:
            raise GovernanceError(
                "governance scope names an unknown catalog endpoint"
            ) from None
        if not endpoint.live_enabled or endpoint.safety_class != "read_only":
            raise GovernanceError("governance scope names a non-live-read endpoint")
        key = _endpoint_key(endpoint)
        if key in approved:
            raise GovernanceError("governance scope contains duplicate endpoints")
        approved.add(key)
    return frozenset(approved)


def _validate_review(value: object) -> dict[str, str]:
    review = _as_mapping(value, "review")
    _require_keys(review, "review", {"identity", "reference"})
    identity = _as_nonempty_string(review["identity"], "review identity")
    reference = _as_nonempty_string(review["reference"], "review reference")
    if not _REVIEW_REFERENCE_RE.fullmatch(reference):
        raise GovernanceError(
            "governance review reference must be a quant_engine issue comment URL"
        )
    return {"identity": identity, "reference": reference}


@dataclass(slots=True)
class LiveReadGovernance:
    """A consumed local operator attestation for a bounded set of calls."""

    receipt_path: Path
    evidence_sha256: str
    operation: str
    mode: str | None
    approved_endpoints: frozenset[str]
    max_requests: int
    review: dict[str, str]
    _used_requests: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reserve_request(self, endpoint: Endpoint) -> None:
        """Durably reserve one approved request before the caller does I/O."""
        key = _endpoint_key(endpoint)
        with self._lock:
            if key not in self.approved_endpoints:
                raise GovernanceError("endpoint is not approved by live-read scope")
            if self._used_requests >= self.max_requests:
                raise GovernanceError("live-read request budget is exhausted")
            self._used_requests += 1
            receipt = {
                "schema_version": 1,
                "evidence_sha256": self.evidence_sha256,
                "operation": self.operation,
                "mode": self.mode,
                "review": self.review,
                "approved_endpoints": sorted(self.approved_endpoints),
                "max_requests": self.max_requests,
                "used_requests": self._used_requests,
                "retry_count": 0,
            }
            _secure_write_receipt(
                self.receipt_path,
                (canonical_json(receipt) + "\n").encode("utf-8"),
            )


def authorize_live_read(
    evidence_path: str | Path,
    *,
    operation: str,
    mode: str | None,
    now: Callable[[], datetime] | None = None,
) -> LiveReadGovernance:
    """Validate and consume one local operator-provided attestation artifact.

    The review reference is shape-checked but is not remotely authenticated by
    this local boundary; callers must operate in the trusted-workspace model
    documented in ``docs/providers/matrixport.md``.
    """
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
    root = _as_mapping(document, "evidence")
    _require_keys(
        root,
        "evidence",
        {
            "schema_version",
            "operation",
            "mode",
            "scope",
            "review",
            "authority",
            "terms",
            "catalog",
            "metering",
        },
    )
    if root["schema_version"] != 1:
        raise GovernanceError("unsupported live-read governance schema")
    if operation not in {"auth-test", "extract", "run"}:
        raise GovernanceError("unsupported live-read governance operation")
    if _as_nonempty_string(root["operation"], "operation") != operation:
        raise GovernanceError(
            "governance operation does not match the requested live read"
        )
    if root["mode"] != mode:
        raise GovernanceError("governance mode does not match the requested live read")
    if operation == "auth-test" and mode is not None:
        raise GovernanceError("auth-test governance mode must be null")
    if operation == "extract" and mode not in {"full", "incremental"}:
        raise GovernanceError("extract governance mode must be full or incremental")
    if operation == "run" and mode != "full":
        raise GovernanceError("run governance mode must be full")
    approved_endpoints = _parse_scope(root["scope"])
    if operation == "auth-test" and approved_endpoints != {
        "GET /mapi/v1/wallet/balance"
    }:
        raise GovernanceError("auth-test scope is not approved for its exact operation")
    review = _validate_review(root["review"])

    authority = _as_mapping(root["authority"], "authority")
    _require_keys(authority, "authority", {"status", "issued_at", "expires_at"})
    if authority["status"] != "CURRENT":
        raise GovernanceError("live-read authority is not CURRENT")
    issued_at = _parse_time(authority["issued_at"], "authority issued_at")
    expires_at = _parse_time(authority["expires_at"], "authority expires_at")
    current = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    if issued_at > current:
        raise GovernanceError("live-read authority is not yet current")
    if expires_at <= current:
        raise GovernanceError("live-read authority is expired")

    terms = _as_mapping(root["terms"], "terms")
    _require_keys(terms, "terms", {"version", "sha256"})
    if terms["version"] != TERMS_VERSION or terms["sha256"] != TERMS_SHA256:
        raise GovernanceError("live-read governance terms do not match accepted terms")
    catalog = _as_mapping(root["catalog"], "catalog")
    _require_keys(catalog, "catalog", {"sha256"})
    if catalog["sha256"] != catalog_sha256():
        raise GovernanceError(
            "live-read governance catalog does not match this adapter"
        )
    metering = _as_mapping(root["metering"], "metering")
    _require_keys(metering, "metering", {"max_requests", "retry_count"})
    max_requests = metering["max_requests"]
    if isinstance(max_requests, bool) or not isinstance(max_requests, int):
        raise GovernanceError("governance max_requests must be an integer")
    if not 1 <= max_requests <= _MAX_REQUESTS:
        raise GovernanceError("governance max_requests is outside the allowed range")
    if metering["retry_count"] != 0:
        raise GovernanceError("governance retry_count must be exactly zero")

    evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    receipt_root = _prepare_canonical_state_root(_canonical_state_root())
    receipt_path = receipt_root / f"{evidence_sha256}.json"
    initial_receipt = {
        "schema_version": 1,
        "evidence_sha256": evidence_sha256,
        "operation": operation,
        "mode": mode,
        "review": review,
        "approved_endpoints": sorted(approved_endpoints),
        "max_requests": max_requests,
        "used_requests": 0,
        "retry_count": 0,
    }
    try:
        _create_receipt_exclusively(
            receipt_path, (canonical_json(initial_receipt) + "\n").encode("utf-8")
        )
    except FileExistsError:
        raise GovernanceError(
            "live-read governance evidence is already consumed"
        ) from None
    except Exception:
        # A partial receipt is deliberately retained: reuse after an interrupted
        # consumption attempt would be less safe than requiring new approval.
        raise GovernanceError(
            "live-read governance receipt could not be persisted"
        ) from None
    return LiveReadGovernance(
        receipt_path=receipt_path,
        evidence_sha256=evidence_sha256,
        operation=operation,
        mode=mode,
        approved_endpoints=approved_endpoints,
        max_requests=max_requests,
        review=review,
    )


def _create_receipt_exclusively(path: Path, content: bytes) -> None:
    """Create, flush, and commit an initial receipt without allowing replay."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    _commit_receipt_directory_entry(path)


def _secure_write_receipt(path: Path, content: bytes) -> None:
    """Replace one receipt with a mode-0600, durable same-directory temporary."""
    if path.is_symlink():
        raise GovernanceError("live-read governance receipt cannot be a symlink")
    temporary = path.with_name(f".{path.name}-{secrets.token_hex(16)}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_receipt(temporary, path)
        _commit_receipt_directory_entry(path)
    except GovernanceError:
        raise
    except Exception:
        raise GovernanceError(
            "live-read governance receipt could not be durably updated"
        ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _replace_receipt(source: Path, destination: Path) -> None:
    """Atomically replace a receipt and request platform-specific durability."""
    if os.name != "nt":
        os.replace(source, destination)
        return

    import ctypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong)
    move_file_ex.restype = ctypes.c_int
    movefile_replace_existing = 0x1
    movefile_write_through = 0x8
    if not move_file_ex(
        str(source),
        str(destination),
        movefile_replace_existing | movefile_write_through,
    ):
        raise OSError(ctypes.get_last_error(), "MoveFileExW failed")


def _commit_receipt_directory_entry(receipt_path: Path) -> None:
    """Durably commit a receipt's containing-directory entry.

    POSIX uses an fsync on the containing directory after create or replace.
    Windows does not support that POSIX primitive for directory handles, so the
    Windows path uses MoveFileExW with MOVEFILE_WRITE_THROUGH for replacements
    and FlushFileBuffers on a write-through receipt handle after each entry
    change.  Both branches fail closed when their durability operation fails.
    """
    if os.name == "nt":
        _flush_windows_receipt_entry(receipt_path)
        return
    if os.name != "posix":
        raise GovernanceError("receipt directory durability is unsupported")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(receipt_path.parent, flags)
    except OSError:
        raise GovernanceError(
            "receipt directory could not be opened for durability"
        ) from None
    try:
        os.fsync(descriptor)
    except OSError:
        raise GovernanceError(
            "receipt directory durability could not be established"
        ) from None
    finally:
        os.close(descriptor)


def _flush_windows_receipt_entry(receipt_path: Path) -> None:
    """Flush Windows receipt data and metadata after a name entry transition."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    generic_write = 0x40000000
    share_all = 0x7
    open_existing = 3
    file_attribute_normal = 0x80
    file_flag_write_through = 0x80000000
    handle = create_file(
        str(receipt_path),
        generic_write,
        share_all,
        None,
        open_existing,
        file_attribute_normal | file_flag_write_through,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise GovernanceError("receipt entry could not be opened for durability")
    flush = kernel32.FlushFileBuffers
    flush.argtypes = (wintypes.HANDLE,)
    flush.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    try:
        if not flush(handle):
            raise GovernanceError("receipt entry durability could not be established")
    finally:
        close(handle)


def _canonical_state_root() -> Path:
    """Return the deterministic per-user receipt root for Matrixport reads."""
    try:
        if os.name == "nt":
            raw_base = os.environ.get("LOCALAPPDATA")
            if not raw_base:
                raise GovernanceError("LOCALAPPDATA is required for live-read receipts")
            base = Path(raw_base)
        else:
            raw_base = os.environ.get("XDG_STATE_HOME")
            base = Path(raw_base) if raw_base else Path.home() / ".local" / "state"
    except (OSError, RuntimeError):
        raise GovernanceError(
            "live-read receipt state base cannot be resolved"
        ) from None
    if not base.is_absolute() or not base.is_dir():
        raise GovernanceError("live-read receipt state base is not a valid directory")
    _reject_reparse_ancestors(base)
    try:
        resolved_base = base.resolve(strict=True)
        if not base.samefile(resolved_base):
            raise GovernanceError(
                "live-read receipt state base cannot use a reparse path"
            )
    except OSError:
        raise GovernanceError(
            "live-read receipt state base cannot be resolved"
        ) from None
    return (
        resolved_base
        / "quant_engine"
        / "matrixport"
        / "governance"
        / "live-read-receipts"
    )


def _prepare_canonical_state_root(state_root: Path) -> Path:
    """Create and verify the receipt directory without accepting link traversal."""
    root = Path(os.path.abspath(os.fspath(state_root)))
    _reject_reparse_ancestors(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise GovernanceError(
            "live-read receipt state root cannot be created"
        ) from None
    _reject_reparse_ancestors(root)
    return root


def _is_reparse_point(path: Path) -> bool:
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
        if current.exists() and _is_reparse_point(current):
            raise GovernanceError(
                "governed receipt workspace cannot use a reparse ancestor"
            )
