"""Emit allowlisted aggregate/hash evidence without serializing private values."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Final, Sequence


_LABEL: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_OPERATIONS: Final = frozenset({"count", "hash", "present", "type"})
_REPARSE_POINT_ATTRIBUTE: Final = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class EvidenceSummaryError(RuntimeError):
    """A stable, value-free evidence summary failure."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _file_evidence(path: Path) -> tuple[bytes, str]:
    candidate = Path(path.anchor) if path.is_absolute() else Path.cwd()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    metadata: os.stat_result | None = None
    for part in parts:
        candidate /= part
        try:
            metadata = os.lstat(candidate)
        except OSError as exc:
            raise EvidenceSummaryError("INPUT_READ_FAILED") from exc
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
        ):
            raise EvidenceSummaryError("INPUT_LINK_FORBIDDEN")
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise EvidenceSummaryError("INPUT_NOT_REGULAR_FILE")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (metadata.st_dev, metadata.st_ino):
            raise EvidenceSummaryError("INPUT_CHANGED_DURING_OPEN")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read()
    finally:
        os.close(descriptor)
    return content, hashlib.sha256(content).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceSummaryError("INPUT_JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _decode_pointer(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/") or pointer == "/":
        raise EvidenceSummaryError("QUERY_POINTER_INVALID")
    segments: list[str] = []
    for raw in pointer[1:].split("/"):
        segment = raw.replace("~1", "/").replace("~0", "~")
        if not segment or segment in {".", ".."}:
            raise EvidenceSummaryError("QUERY_POINTER_INVALID")
        segments.append(segment)
    return tuple(segments)


def _parse_query(value: str) -> tuple[str, tuple[str, ...], str]:
    parts = value.split(":", 2)
    if len(parts) != 3:
        raise EvidenceSummaryError("QUERY_INVALID")
    label, pointer, operation = parts
    if _LABEL.fullmatch(label) is None:
        raise EvidenceSummaryError("QUERY_LABEL_INVALID")
    if operation not in _OPERATIONS:
        raise EvidenceSummaryError("QUERY_OPERATION_INVALID")
    return label, _decode_pointer(pointer), operation


def _resolve(value: object, segments: Sequence[str]) -> object:
    current = value
    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                raise EvidenceSummaryError("QUERY_FIELD_UNKNOWN")
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdecimal():
            index = int(segment)
            if index >= len(current):
                raise EvidenceSummaryError("QUERY_FIELD_UNKNOWN")
            current = current[index]
            continue
        raise EvidenceSummaryError("QUERY_FIELD_UNKNOWN")
    return current


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    raise EvidenceSummaryError("QUERY_VALUE_UNSUPPORTED")


def summarize(path: Path, queries: Sequence[str]) -> dict[str, object]:
    if not queries:
        raise EvidenceSummaryError("QUERY_REQUIRED")
    parsed = [_parse_query(query) for query in queries]
    labels = [label for label, _, _ in parsed]
    if len(labels) != len(set(labels)):
        raise EvidenceSummaryError("QUERY_LABEL_DUPLICATE")
    content, digest = _file_evidence(path)
    try:
        document = json.loads(content, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceSummaryError("INPUT_JSON_INVALID") from exc

    results: dict[str, object] = {}
    for label, pointer, operation in parsed:
        selected = _resolve(document, pointer)
        if operation == "count":
            if not isinstance(selected, (dict, list)):
                raise EvidenceSummaryError("QUERY_COUNT_REQUIRES_COLLECTION")
            results[label] = {"count": len(selected), "operation": operation}
        elif operation == "hash":
            encoded = _canonical_json(selected)
            results[label] = {
                "bytes": len(encoded),
                "operation": operation,
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        elif operation == "present":
            results[label] = {"operation": operation, "present": True}
        else:
            results[label] = {
                "operation": operation,
                "value_type": _type_name(selected),
            }
    return {
        "input_bytes": len(content),
        "input_sha256": digest,
        "queries": results,
        "state": "SANITIZED_AGGREGATE_ONLY",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--query", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        result = summarize(args.input, args.query)
    except (OSError, EvidenceSummaryError) as exc:
        code = (
            str(exc) if isinstance(exc, EvidenceSummaryError) else "INPUT_READ_FAILED"
        )
        print(f"restricted-evidence-summary: FAIL: {code}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
