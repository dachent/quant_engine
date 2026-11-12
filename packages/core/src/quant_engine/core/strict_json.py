"""One fail-closed JSON decoder for authority bytes."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import OrderedDict
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

    def reject_overflow_float(number: str) -> float:
        try:
            parsed = float(number)
        except OverflowError:
            raise _error(label, "contains nonfinite JSON number") from None
        if not math.isfinite(parsed):
            raise _error(label, "contains nonfinite JSON number")
        return parsed

    try:
        return json.loads(
            decoded,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_nonfinite,
            parse_float=reject_overflow_float,
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


# ---------------------------------------------------------------------------
# Shared read-only parse: decode identical bytes once per process.
#
# The strict decode (duplicate-member hook) costs seconds on the 50 MB provider
# ledger and the account-manifest verifier asks for the same bytes dozens of
# times. ``strict_json_shared`` runs the full strict decode on the first request
# for a given byte string and hands every later request the *same* parsed
# object. Equality is on the full bytes (the dict compares contents, not a
# digest), failures are never cached, so tampered or malformed bytes always run
# the strict decode and raise.
#
# The price of sharing is that no caller may mutate the result. That is enforced,
# not assumed: the first hand-out records a digest of the parsed tree and
# ``verify_shared_unmutated`` recomputes it, raising if any caller changed it.
# Use it only where the caller reads and does not modify the object.

_SHARED_MIN_BYTES = 1 << 20
_SHARED_BUDGET_BYTES = 1 << 30
_shared: OrderedDict[bytes, tuple[object, str]] = OrderedDict()
_shared_bytes = 0
_shared_lock = threading.Lock()
_decode_calls = 0


def _tree_digest(parsed: object) -> str:
    encoded = json.dumps(
        parsed, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def strict_json_shared(value: bytes, *, label: str) -> object:
    """Like :func:`strict_json_loads`, but identical bytes decode once; read-only."""

    global _shared_bytes, _decode_calls
    if type(value) is not bytes or len(value) < _SHARED_MIN_BYTES:
        return strict_json_loads(value, label=label)
    with _shared_lock:
        entry = _shared.get(value)
        if entry is not None:
            _shared.move_to_end(value)
            return entry[0]
    _decode_calls += 1
    parsed = strict_json_loads(value, label=label)
    digest = _tree_digest(parsed)
    with _shared_lock:
        entry = _shared.get(value)
        if entry is not None:
            return entry[0]
        _shared[value] = (parsed, digest)
        _shared_bytes += len(value) * 4
        while _shared_bytes > _SHARED_BUDGET_BYTES and len(_shared) > 1:
            old_key, _ = _shared.popitem(last=False)
            _shared_bytes -= len(old_key) * 4
    return parsed


def verify_shared_unmutated() -> None:
    """Raise if any caller mutated an object handed out by ``strict_json_shared``."""

    with _shared_lock:
        entries = list(_shared.values())
    for parsed, digest in entries:
        if _tree_digest(parsed) != digest:
            raise StrictJSONError("shared parsed JSON was mutated by a caller")


def clear_strict_json_shared() -> None:
    global _shared_bytes
    with _shared_lock:
        _shared.clear()
        _shared_bytes = 0
