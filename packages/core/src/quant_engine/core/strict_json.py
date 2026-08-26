"""One fail-closed JSON decoder for authority bytes."""

from __future__ import annotations

import json
from typing import Any


class StrictJSONError(ValueError):
    """JSON source bytes are not unambiguous authority input."""


def _error(label: str, detail: str) -> StrictJSONError:
    return StrictJSONError(f"{label}: {detail}")


def strict_json_loads(value: bytes, *, label: str) -> object:
    """Decode strict UTF-8 JSON without duplicate keys or nonfinite values."""

    try:
        decoded = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _error(label, "must be valid UTF-8 JSON") from exc

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise _error(label, "contains a duplicate object member")
            result[key] = item
        return result

    def reject_nonfinite(constant: str) -> object:
        raise _error(label, f"contains nonfinite JSON constant {constant}")

    try:
        return json.loads(
            decoded,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_nonfinite,
        )
    except StrictJSONError:
        raise
    except json.JSONDecodeError as exc:
        raise _error(label, "must be valid JSON") from exc


def strict_json_object(value: bytes, *, label: str) -> dict[str, object]:
    """Decode strict JSON and require an object root."""

    parsed = strict_json_loads(value, label=label)
    if not isinstance(parsed, dict):
        raise _error(label, "root must be a JSON object")
    return parsed
