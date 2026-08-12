"""Emit allowlisted aggregate/hash evidence without serializing private values."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Final, Sequence


_LABEL: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_OPERATIONS: Final = frozenset({"count", "hash", "present", "type"})


class EvidenceSummaryError(RuntimeError):
    """A stable, value-free evidence summary failure."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _file_evidence(path: Path) -> tuple[bytes, str]:
    if path.is_symlink() or not path.is_file():
        raise EvidenceSummaryError("INPUT_NOT_REGULAR_FILE")
    content = path.read_bytes()
    return content, hashlib.sha256(content).hexdigest()


def _decode_pointer(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/") or pointer == "/":
        raise EvidenceSummaryError("QUERY_POINTER_INVALID")
    tokens: list[str] = []
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if not token or token in {".", ".."}:
            raise EvidenceSummaryError("QUERY_POINTER_INVALID")
        tokens.append(token)
    return tuple(tokens)


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


def _resolve(value: object, tokens: Sequence[str]) -> object:
    current = value
    for token in tokens:
        if isinstance(current, dict):
            if token not in current:
                raise EvidenceSummaryError("QUERY_FIELD_UNKNOWN")
            current = current[token]
            continue
        if isinstance(current, list) and token.isdecimal():
            index = int(token)
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
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
        code = str(exc) if isinstance(exc, EvidenceSummaryError) else "INPUT_READ_FAILED"
        print(f"restricted-evidence-summary: FAIL: {code}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
