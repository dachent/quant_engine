"""The replica gate: a real-roots ``execute --commit`` needs a passed replica ceremony.

The release hash is shown at the real-roots dry-run (PREPARED); the replica ceremony
runs beside it. The owner may confirm the hash early, but the irreversible commit on the
real roots stays blocked until the replica of the same release (same code commit, same
release generations, same node topology) has committed, post-verified, rehearsed a
mid-commit rollback and restored in a clean room.

Two artifacts exist:

* a replica root marker, ``.replica-root.json``, written last by the replica copy; a
  pipeline spec whose coordination root and every release root sit under a marked
  directory is a replica spec and may commit without a receipt;
* a ceremony receipt, written only by ``tools/release_replica.py ceremony``, which a
  real-roots commit must present and which :func:`verify_receipt` checks against the
  real spec.

A hand-written receipt or marker defeats this gate; it is a guard against skipped steps
and stale or mismatched replicas, not against deliberate forgery, and the receipt names
the digest of every step log so a forgery has to fabricate those too.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final, Mapping

from .contracts import canonical_json_bytes
from .pipeline import PipelineSpec

REPLICA_MARKER_NAME: Final = ".replica-root.json"
REPLICA_MARKER_KIND: Final = "quant-engine-replica-root"
RECEIPT_KIND: Final = "quant-engine-replica-ceremony-receipt"
SCHEMA_VERSION: Final = 1

# Step names, in ceremony order. Every one must have run with exit code 0.
REQUIRED_STEPS: Final[tuple[str, ...]] = (
    "preflight",
    "dry-run",
    "validation-findings",
    "commit",
    "post-release-verify",
    "recovery-rehearsal",
    "clean-room-restore",
)


class ReplicaGateError(ValueError):
    """The replica gate refuses; the message names the first failed condition."""


def topology(spec: PipelineSpec) -> dict[str, Any]:
    """Everything about a spec that must be equal between the real run and its replica.

    Paths differ by design (the replica lives elsewhere) and so does the spec digest;
    the code commit, the proposed release generations and the node ids may not.
    """

    node_ids = sorted(node.node_id for node in spec.nodes)
    return {
        "code_commit": spec.code_commit,
        "node_count": len(node_ids),
        "node_ids_sha256": hashlib.sha256(canonical_json_bytes(node_ids)).hexdigest(),
        "release_generations": dict(sorted(spec.release_generations.items())),
        "target_count": len(spec.release_generations),
    }


def _read_object(path: Path, what: str) -> dict[str, Any]:
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
    marker = _read_object(path, "replica root marker")
    if (
        marker.get("kind") != REPLICA_MARKER_KIND
        or marker.get("schema_version") != SCHEMA_VERSION
    ):
        raise ReplicaGateError(f"replica root marker differs: {path}")
    return marker


def _marked_ancestor(path: Path) -> Path | None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if read_marker(candidate) is not None:
            return candidate
    return None


def is_replica_spec(spec: PipelineSpec) -> bool:
    """True when the coordination root and every release root sit under one marker."""

    anchor = _marked_ancestor(spec.coordination_root)
    if anchor is None:
        return False
    for root in spec.roots.values():
        if not root.resolve().is_relative_to(anchor):
            return False
    return True


def build_receipt(
    *,
    real_spec: PipelineSpec,
    replica_spec: PipelineSpec,
    steps: list[dict[str, Any]],
    commit_result: Mapping[str, Any],
    harness_code_commit: str,
) -> dict[str, Any]:
    """The receipt body; the ceremony tool supplies the observed step outcomes."""

    return {
        "commit_result": {
            "release_id": commit_result.get("release_id"),
            "release_state": commit_result.get("release_state"),
            "spec_sha256": commit_result.get("spec_sha256"),
        },
        "harness_code_commit": harness_code_commit,
        "kind": RECEIPT_KIND,
        "real_topology": topology(real_spec),
        "replica_spec_sha256": replica_spec.spec_sha256,
        "replica_topology": topology(replica_spec),
        "schema_version": SCHEMA_VERSION,
        "steps": steps,
    }


def verify_receipt(path: Path | None, spec: PipelineSpec) -> dict[str, Any]:
    """Refuse unless ``path`` is a complete ceremony receipt for ``spec``'s release."""

    if path is None:
        raise ReplicaGateError(
            "a real-roots commit requires --replica-receipt from a passed replica "
            "ceremony (tools/release_replica.py ceremony)"
        )
    receipt = _read_object(path, "replica receipt")
    if (
        receipt.get("kind") != RECEIPT_KIND
        or receipt.get("schema_version") != SCHEMA_VERSION
    ):
        raise ReplicaGateError("replica receipt kind or schema version differs")
    steps = receipt.get("steps")
    if not isinstance(steps, list):
        raise ReplicaGateError("replica receipt carries no steps")
    by_name: dict[str, Mapping[str, Any]] = {}
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("name"), str):
            raise ReplicaGateError("replica receipt step is malformed")
        by_name[str(step["name"])] = step
    for name in REQUIRED_STEPS:
        step = by_name.get(name)
        if step is None:
            raise ReplicaGateError(f"replica ceremony step missing: {name}")
        if step.get("rc") != 0 or isinstance(step.get("rc"), bool):
            raise ReplicaGateError(f"replica ceremony step failed: {name}")
        for key in ("stdout_sha256", "stderr_sha256"):
            value = step.get(key)
            if not isinstance(value, str) or len(value) != 64:
                raise ReplicaGateError(f"replica ceremony step lacks {key}: {name}")
    commit_result = receipt.get("commit_result")
    if (
        not isinstance(commit_result, dict)
        or commit_result.get("release_state") != "COMMITTED"
    ):
        raise ReplicaGateError("replica release is not COMMITTED")
    wanted = topology(spec)
    if receipt.get("real_topology") != wanted:
        raise ReplicaGateError(
            "replica receipt was issued for a different real release "
            "(code commit, release generations or node ids differ)"
        )
    if receipt.get("replica_topology") != wanted:
        raise ReplicaGateError("replica ran a different topology than the real spec")
    if receipt.get("harness_code_commit") != spec.code_commit:
        raise ReplicaGateError("replica rehearsal harness commit differs from the spec")
    return receipt
