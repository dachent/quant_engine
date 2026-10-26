"""One framing for a directory's file-inventory identity (MAINT-CONSOLIDATE-001).

Four producers (panel authorization roots, the pipeline materializer, the release
builder and attended preparation) each hash a directory as canonical JSON rows
plus a trailing newline. They drifted once (two omitted the newline and every
receipt became unverifiable), so the framing now lives here. Each producer keeps
its own link policy and error type, because refusal behavior is not part of the
identity; only the bytes that are hashed are shared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json


def inventory_identity(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """``{"file_count", "inventory_sha256"}`` for rows of path/sha256/size_bytes."""

    encoded = (
        json.dumps(list(rows), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")
    return {
        "file_count": len(rows),
        "inventory_sha256": hashlib.sha256(encoded).hexdigest(),
    }
