"""Provider-neutral, byte-preserving HAR evidence receipts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
from typing import Any, NoReturn


_MANIFEST_SCHEMA = "quant-engine.web-contract-extraction.v1"


class HarParseStatus(StrEnum):
    """The parse state reported after a HAR source has been preserved."""

    SOURCE_PRESERVED = "SOURCE_PRESERVED"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INVALID_UTF8 = "INVALID_UTF8"
    INVALID_JSON = "INVALID_JSON"
    INVALID_HAR = "INVALID_HAR"


@dataclass(frozen=True)
class HarExtractionReceipt:
    """Value-safe result of a create-only local HAR preservation operation."""

    output_dir: Path
    source_path: Path
    manifest_path: Path
    source_sha256: str
    source_size_bytes: int
    parse_status: HarParseStatus
    materialized_entry_count: int


def _source_bytes(source: object) -> tuple[bytes, str]:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source), "source.har"
    if isinstance(source, memoryview):
        return source.tobytes(), "source.har"
    if isinstance(source, (str, os.PathLike)):
        source_path = Path(source)
        return source_path.read_bytes(), "source.har"
    raise TypeError("source must be a filesystem path or bytes-like value")


def _write_flushed(path: Path, value: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _manifest_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _publish_manifest(path: Path, value: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(".json.tmp")
    _write_flushed(temporary_path, _manifest_bytes(value))
    os.replace(temporary_path, path)


def _manifest(
    *,
    source_filename: str,
    source_sha256: str,
    source_size_bytes: int,
    parse_status: HarParseStatus,
    source_entry_count: int,
    materialized_entry_count: int,
    diagnostic_codes: list[str],
) -> dict[str, Any]:
    return {
        "completeness": (
            "SOURCE_PRESERVED"
            if parse_status is HarParseStatus.SOURCE_PRESERVED
            else "COMPLETE"
            if parse_status is HarParseStatus.COMPLETE
            else "INCOMPLETE"
        ),
        "diagnostic_codes": diagnostic_codes,
        "materialized_entry_count": materialized_entry_count,
        "parse_status": parse_status.value,
        "schema": _MANIFEST_SCHEMA,
        "source": {
            "filename": source_filename,
            "sha256": source_sha256,
            "size_bytes": source_size_bytes,
        },
        "source_entry_count": source_entry_count,
    }


def _reject_non_finite_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant: {value}")


def _parse_status(source: bytes) -> tuple[HarParseStatus, int, list[str]]:
    try:
        text = source.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return HarParseStatus.INVALID_UTF8, 0, [HarParseStatus.INVALID_UTF8.value]
    try:
        document = json.loads(text, parse_constant=_reject_non_finite_constant)
    except (json.JSONDecodeError, ValueError):
        return HarParseStatus.INVALID_JSON, 0, [HarParseStatus.INVALID_JSON.value]
    if not isinstance(document, dict):
        return HarParseStatus.INVALID_HAR, 0, [HarParseStatus.INVALID_HAR.value]
    log = document.get("log")
    if not isinstance(log, dict):
        return HarParseStatus.INVALID_HAR, 0, [HarParseStatus.INVALID_HAR.value]
    entries = log.get("entries")
    if not isinstance(entries, list):
        return HarParseStatus.INVALID_HAR, 0, [HarParseStatus.INVALID_HAR.value]
    if entries:
        return HarParseStatus.PARTIAL, len(entries), ["ENTRIES_NOT_MATERIALIZED"]
    return HarParseStatus.COMPLETE, len(entries), []


def extract_har(
    source: str | os.PathLike[str] | bytes | bytearray | memoryview,
    output_dir: str | os.PathLike[str],
) -> HarExtractionReceipt:
    """Preserve source bytes and return a safe receipt without provider semantics.

    Slice 1 establishes source preservation and parse receipts only; it does not
    yet materialize parsed entries or their body derivatives.
    """

    source_bytes, source_filename = _source_bytes(source)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=False)
    source_path = destination / "source.har"
    manifest_path = destination / "manifest.json"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source_size_bytes = len(source_bytes)

    _write_flushed(source_path, source_bytes)
    _publish_manifest(
        manifest_path,
        _manifest(
            source_filename=source_filename,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
            parse_status=HarParseStatus.SOURCE_PRESERVED,
            source_entry_count=0,
            materialized_entry_count=0,
            diagnostic_codes=[],
        ),
    )
    parse_status, source_entry_count, diagnostic_codes = _parse_status(source_bytes)
    _publish_manifest(
        manifest_path,
        _manifest(
            source_filename=source_filename,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
            parse_status=parse_status,
            source_entry_count=source_entry_count,
            materialized_entry_count=0,
            diagnostic_codes=diagnostic_codes,
        ),
    )
    return HarExtractionReceipt(
        output_dir=destination,
        source_path=source_path,
        manifest_path=manifest_path,
        source_sha256=source_sha256,
        source_size_bytes=source_size_bytes,
        parse_status=parse_status,
        materialized_entry_count=0,
    )
