"""The replica root marker and the directory-identity map a replica copy carries.

A replica root is a scratch copy of the live roots (``tools/release_replica.py copy``).
Its marker ``.replica-root.json`` is written last. A committed predecessor release records
the directory identity (volume serial and file index) of the REAL roots and companion
parent directories, which can never equal the identity of a copy. The marker therefore
carries ``identity_map``: for every copied directory the identity the real directory had
(``source``) and the identity of its copy (``copied``). The release layer accepts a
recorded identity for a directory under a marker only when the pair (recorded, computed)
is in that map, so an identity change AFTER the copy is still refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

REPLICA_MARKER_NAME: Final = ".replica-root.json"
REPLICA_MARKER_KIND: Final = "quant-engine-replica-root"
MARKER_SCHEMA_VERSION: Final = 1


class ReplicaGateError(ValueError):
    """A replica gate or marker refusal; the message names the failed condition."""


def read_object(path: Path, what: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ReplicaGateError(f"{what} is not a regular file: {path}")
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplicaGateError(f"{what} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ReplicaGateError(f"{what} root must be an object: {path}")
    return value


def read_marker(directory: Path) -> dict[str, Any] | None:
    path = directory / REPLICA_MARKER_NAME
    if not path.exists():
        return None
    marker = read_object(path, "replica root marker")
    if (
        marker.get("kind") != REPLICA_MARKER_KIND
        or marker.get("schema_version") != MARKER_SCHEMA_VERSION
    ):
        raise ReplicaGateError(f"replica root marker differs: {path}")
    return marker


def marked_ancestor(path: Path) -> Path | None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if read_marker(candidate) is not None:
            return candidate
    return None


def root_carries_marker(root: Path) -> bool:
    """A governed release root must never itself contain a replica marker (the marker
    sits above the roots of a replica). Fail-closed signal for real-root checks."""

    return (root / REPLICA_MARKER_NAME).exists()


def _honored_marker(path: Path) -> dict[str, Any] | None:
    """The marker above ``path`` when it may be trusted: the path is strictly inside the
    marked directory, the marker names the workspace it was copied from, and that source
    workspace is disjoint from the marked directory (a marker planted inside the real
    governed workspace, or one that contains it, is ignored)."""

    resolved = path.resolve()
    for anchor in resolved.parents:
        marker = read_marker(anchor)
        if marker is None:
            continue
        source = marker.get("source_workspace")
        if not isinstance(source, str):
            return None
        src = Path(source).resolve()
        if src == anchor or src in anchor.parents or anchor in src.parents:
            return None
        return marker
    return None


def translate_identity(path: Path, recorded: str | None, computed: str) -> str | None:
    """``computed`` when ``path`` is a marked copy whose map pairs (recorded, computed).

    The pair must be one the copy recorded: ``source`` equal to the identity the
    committed manifest recorded for this directory, ``copied`` equal to the identity the
    copy has now. Otherwise ``recorded`` is returned unchanged and the caller refuses.
    """

    if recorded is None or recorded == computed:
        return recorded
    marker = _honored_marker(path)
    if marker is None:
        return recorded
    for entry in marker.get("identity_map", []):
        if (
            isinstance(entry, dict)
            and entry.get("source") == recorded
            and entry.get("copied") == computed
        ):
            return computed
    return recorded
