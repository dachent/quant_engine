"""Immutable storage for exact HTTP response bytes and safe request envelopes."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .storage import RunStorage, StorageError
from .signing import encode_query
from .util import canonical_json, sha256_bytes, sha256_text, utc_now_ms


_SENSITIVE_NAME = re.compile(
    r"(?i)(authorization|cookie|credential|password|private.?key|secret|signature|access.?key|api.?key|token)"
)


class RawStoreError(RuntimeError):
    """Raised when immutable raw evidence is inconsistent or unsafe."""


@dataclass(frozen=True)
class StoredResponse:
    request_id: str
    request_hash: str
    response_sha256: str
    response_path: Path
    envelope_path: Path
    resumed: bool


def request_fingerprint(
    endpoint: str, safe_params: Mapping[str, Any], method: str = "GET"
) -> str:
    """Hash the semantic request after exact query encoding has been finalized.

    ``safe_params`` should therefore be the query representation sent on the
    wire (a mapping whose list values retain their order), not an earlier set of
    defaults.  The fingerprint contains no authentication headers.
    """
    if method != "GET":
        raise ValueError("RawStore accepts GET requests only")
    return sha256_text(
        canonical_json(
            {
                "method": method,
                "endpoint": endpoint,
                "encoded_query": encode_query(safe_params),
            }
        )
    )


def _assert_safe(value: Any, location: str = "envelope") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SENSITIVE_NAME.search(str(key)):
                raise RawStoreError(f"sensitive key is forbidden in {location}: {key}")
            _assert_safe(item, f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_safe(item, f"{location}[{index}]")


def _atomic_write_once(path: Path, data: bytes) -> bool:
    """Atomically create ``path``; verify rather than overwrite on replay."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise RawStoreError(f"immutable file differs: {path}")
        return False
    # Keep the temporary basename short: the selected Windows OneDrive path is
    # already long enough that repeating a 64-character digest can exceed the
    # legacy MAX_PATH boundary in Python 3.10.
    temporary = path.with_name(
        f".{path.stem[:8]}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # There is one writer per run.  replace provides atomic publication; the
        # pre-existence check above protects the immutable replay contract.
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


class RawStore:
    """Preserve one exact byte payload and one sanitized envelope per request."""

    def __init__(
        self,
        root: str | Path,
        *,
        storage: RunStorage | None = None,
        run_id: str | None = None,
    ):
        self.root = Path(root)
        self.responses_dir = self.root / "responses"
        self.envelopes_dir = self.root / "envelopes"
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.envelopes_dir.mkdir(parents=True, exist_ok=True)
        self.storage = storage
        self.run_id = run_id
        if (storage is None) != (run_id is None):
            raise ValueError("storage and run_id must be supplied together")

    def _paths(self, request_hash: str) -> tuple[Path, Path]:
        if not re.fullmatch(r"[0-9a-f]{64}", request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        return (
            self.responses_dir / f"{request_hash}.bin",
            self.envelopes_dir / f"{request_hash}.json",
        )

    def lookup(
        self, endpoint: str, safe_params: Mapping[str, Any]
    ) -> StoredResponse | None:
        """Return durable evidence for a request, allowing extraction to resume."""
        _assert_safe(safe_params, "safe_params")
        digest = request_fingerprint(endpoint, safe_params)
        response_path, envelope_path = self._paths(digest)
        if self.storage is not None and self.run_id is not None:
            record = self.storage.get_request_by_hash(self.run_id, digest)
            if record is None:
                if not response_path.exists() and not envelope_path.exists():
                    return None
                if not response_path.is_file() or not envelope_path.is_file():
                    raise RawStoreError("orphan raw evidence is incomplete")
                envelope = self._read_envelope(envelope_path)
                if envelope.get("request_hash") != digest:
                    raise RawStoreError("orphan envelope request hash mismatch")
                response_sha = sha256_bytes(response_path.read_bytes())
                if response_sha != envelope.get("response_sha256"):
                    raise RawStoreError("orphan raw response hash mismatch")
                # A crash can occur after both files are durable but before the
                # short SQLite insert commits.  Register that exact evidence;
                # never replay the network request with a new request ID.
                self.storage.record_request(self.run_id, envelope)
                return StoredResponse(
                    request_id=str(envelope["request_id"]),
                    request_hash=digest,
                    response_sha256=response_sha,
                    response_path=response_path,
                    envelope_path=envelope_path,
                    resumed=True,
                )
            if not response_path.is_file() or not envelope_path.is_file():
                raise RawStoreError(
                    "SQLite request exists without its durable raw evidence"
                )
            response_sha = sha256_bytes(response_path.read_bytes())
            if response_sha != record["response_sha256"]:
                raise RawStoreError("raw response hash does not match SQLite")
            return StoredResponse(
                request_id=str(record["request_id"]),
                request_hash=digest,
                response_sha256=response_sha,
                response_path=response_path,
                envelope_path=envelope_path,
                resumed=True,
            )
        if not envelope_path.is_file():
            if response_path.exists():
                raise RawStoreError("raw response exists without its envelope")
            return None
        envelope = self._read_envelope(envelope_path)
        if not response_path.is_file():
            raise RawStoreError("raw envelope exists without its response")
        response_sha = sha256_bytes(response_path.read_bytes())
        if response_sha != envelope["response_sha256"]:
            raise RawStoreError("raw response hash does not match its envelope")
        return StoredResponse(
            request_id=str(envelope["request_id"]),
            request_hash=digest,
            response_sha256=response_sha,
            response_path=response_path,
            envelope_path=envelope_path,
            resumed=True,
        )

    def store_response(
        self,
        *,
        endpoint: str,
        safe_params: Mapping[str, Any],
        response_bytes: bytes,
        status_code: int,
        elapsed_ms: int,
        row_count: int,
        request_id: str | None = None,
        requested_at_ms: int | None = None,
        extractor_hash: str = "",
        config_hash: str = "",
        observed_bounds: Mapping[str, Any] | None = None,
        terminal_reason: str | None = None,
    ) -> StoredResponse:
        """Durably publish raw bytes, envelope, then the SQLite request row.

        Checkpoints may safely advance only after this function returns.  If a
        process stops between the file writes and SQLite commit, replay repairs
        the missing database row without modifying the evidence files.
        """
        _assert_safe(safe_params, "safe_params")
        _assert_safe(observed_bounds, "observed_bounds")
        if not endpoint.startswith("/"):
            raise ValueError("endpoint must be an absolute API path")
        if int(elapsed_ms) < 0 or int(row_count) < 0:
            raise ValueError("elapsed_ms and row_count must be non-negative")
        request_hash = request_fingerprint(endpoint, safe_params)
        response_sha = sha256_bytes(response_bytes)
        response_path, envelope_path = self._paths(request_hash)
        resumed = response_path.exists() or envelope_path.exists()

        existing_envelope: dict[str, Any] | None = None
        if envelope_path.exists():
            existing_envelope = self._read_envelope(envelope_path)
            invariants = {
                "request_hash": request_hash,
                "endpoint": endpoint,
                "safe_params": dict(safe_params),
                "response_sha256": response_sha,
                "status_code": int(status_code),
                "row_count": int(row_count),
            }
            if any(
                existing_envelope.get(key) != value for key, value in invariants.items()
            ):
                raise RawStoreError("immutable envelope differs from request replay")

        durable_request_id = (
            str(existing_envelope["request_id"])
            if existing_envelope is not None
            else str(request_id or request_hash[:24])
        )
        if request_id is not None and durable_request_id != str(request_id):
            raise RawStoreError("request_id differs from the durable envelope")

        envelope: dict[str, Any] = existing_envelope or {
            "schema_version": 1,
            "request_id": durable_request_id,
            "request_hash": request_hash,
            "method": "GET",
            "endpoint": endpoint,
            "safe_params": dict(safe_params),
            "status_code": int(status_code),
            "elapsed_ms": int(elapsed_ms),
            "row_count": int(row_count),
            "response_sha256": response_sha,
            "requested_at_ms": int(requested_at_ms or utc_now_ms()),
            "extractor_hash": extractor_hash,
            "config_hash": config_hash,
            "observed_bounds": dict(observed_bounds)
            if observed_bounds is not None
            else None,
            "terminal_reason": terminal_reason,
            "raw_relpath": response_path.relative_to(self.root).as_posix(),
            "envelope_relpath": envelope_path.relative_to(self.root).as_posix(),
        }
        _assert_safe(envelope)

        # Raw bytes are durable before metadata, and both are durable before a
        # checkpoint-visible SQLite request record.
        _atomic_write_once(response_path, response_bytes)
        encoded_envelope = (canonical_json(envelope) + "\n").encode("utf-8")
        _atomic_write_once(envelope_path, encoded_envelope)

        if self.storage is not None and self.run_id is not None:
            try:
                self.storage.record_request(self.run_id, envelope)
            except StorageError:
                # Evidence is intentionally retained for deterministic recovery.
                raise

        return StoredResponse(
            request_id=durable_request_id,
            request_hash=request_hash,
            response_sha256=response_sha,
            response_path=response_path,
            envelope_path=envelope_path,
            resumed=resumed,
        )

    def stage_response(
        self,
        *,
        endpoint: str,
        safe_params: Mapping[str, Any],
        response_bytes: bytes,
    ) -> Path:
        """Durably stage exact response bytes before parsing or validation."""
        _assert_safe(safe_params, "safe_params")
        request_hash = request_fingerprint(endpoint, safe_params)
        response_path, _envelope_path = self._paths(request_hash)
        _atomic_write_once(response_path, response_bytes)
        return response_path

    def read_response(self, request_hash: str) -> bytes:
        response_path, envelope_path = self._paths(request_hash)
        if not response_path.is_file() or not envelope_path.is_file():
            raise FileNotFoundError(request_hash)
        envelope = self._read_envelope(envelope_path)
        data = response_path.read_bytes()
        if sha256_bytes(data) != envelope["response_sha256"]:
            raise RawStoreError("raw response hash mismatch")
        return data

    def iter_envelopes(self) -> Iterator[dict[str, Any]]:
        for path in sorted(self.envelopes_dir.glob("*.json")):
            yield self._read_envelope(path)

    @staticmethod
    def _read_envelope(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RawStoreError(f"invalid raw envelope: {path}") from error
        if not isinstance(value, dict):
            raise RawStoreError(f"raw envelope is not an object: {path}")
        _assert_safe(value)
        return value
