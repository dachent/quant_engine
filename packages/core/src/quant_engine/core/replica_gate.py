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
import os
import json
import re
from pathlib import Path
from typing import Any, Final, Mapping

from .contracts import canonical_json_bytes
from .pipeline import PipelineSpec
from .replica_marker import (
    MARKER_SCHEMA_VERSION,
    REPLICA_MARKER_KIND as REPLICA_MARKER_KIND,
    REPLICA_MARKER_NAME as REPLICA_MARKER_NAME,
    ReplicaGateError,
    marked_ancestor as _marked_ancestor,
    read_marker,
    read_object as _read_object,
)

RECEIPT_KIND: Final = "quant-engine-replica-ceremony-receipt"
SCHEMA_VERSION: Final = MARKER_SCHEMA_VERSION

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


def _path_case_flags() -> int:
    """Windows paths compare without case; on a case-sensitive filesystem two roots that
    differ only by case are different roots and must keep the digest apart."""

    return re.IGNORECASE if os.name == "nt" else 0


def _patterns(*pairs: tuple[Path, str]) -> list[tuple[re.Pattern[str], str]]:
    return [
        (
            re.compile(re.escape(base.as_posix().rstrip("/")), _path_case_flags()),
            token,
        )
        for base, token in pairs
    ]


def _normalize(value: Any, patterns: list[tuple[re.Pattern[str], str]]) -> Any:
    """Value with every path under a given base replaced by that base's token, slashes unified."""

    if isinstance(value, str):
        text = value.replace("\\", "/")
        for pattern, token in patterns:
            text = pattern.sub(token, text)
        return text
    if isinstance(value, list):
        return [_normalize(item, patterns) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item, patterns) for key, item in value.items()}
    return value


def _tokenise_path_fields(value: Any, base: re.Pattern[str]) -> Any:
    """``value`` with ``base`` replaced by ``<ROOT>`` where it is the prefix of a ``path`` field.

    Only a string held under a key named ``path`` is touched, and only when ``base`` is the
    whole leading path (ends at a separator or at the end of the string); the same text in any
    other field, or a different root or relative path, stays raw and keeps the digest apart."""

    if isinstance(value, list):
        return [_tokenise_path_fields(item, base) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "path" and isinstance(item, str):
            text = item.replace("\\", "/")
            match = base.match(text)
            out[key] = "<ROOT>" + text[match.end() :] if match else item
        else:
            out[key] = _tokenise_path_fields(item, base)
    return out


def _normalize_gates(
    body: dict[str, Any], anchor: Path, real_anchor: Path | None = None
) -> None:
    """Gate files sit beside their spec (real ``production`` vs ``replica-production``) and the
    rebind contract embeds root paths, so neither the path nor the raw digest can match.
    Each gate is compared by its file name and by the digest of its content with the gate
    directory and the workspace anchor replaced by tokens; the directory is read from the
    gate's own path, never hardcoded. An unreadable file keeps its recorded digest.

    A replica's gates are produced from the shared real ledger receipt, so they carry the
    real workspace's absolute paths in their ``path`` fields (``finality_evidence.path``);
    for a replica ``real_anchor`` is that real workspace and is tokenised, as a path prefix
    in ``path`` fields only, to the same ``<ROOT>`` the real spec's own anchor becomes."""

    real_base = (
        re.compile(
            re.escape(real_anchor.as_posix().rstrip("/")) + r"(?=/|$)",
            _path_case_flags(),
        )
        if real_anchor is not None
        else None
    )

    for gate in body.get("gates", []):
        path = Path(str(gate["path"]))
        patterns = _patterns((path.parent, "<GATEDIR>"), (anchor, "<ROOT>"))
        gate["path"] = _normalize(str(path), patterns)
        try:
            raw = path.read_bytes()
            try:
                content: Any = _normalize(json.loads(raw.decode("utf-8")), patterns)
                if real_base is not None:
                    content = _tokenise_path_fields(content, real_base)
                gate["sha256"] = hashlib.sha256(
                    canonical_json_bytes(content)
                ).hexdigest()
            except (UnicodeDecodeError, ValueError):
                text = _normalize(raw.decode("utf-8", "replace"), patterns)
                gate["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        except OSError:
            pass


def topology(
    spec: PipelineSpec, anchor: Path, real_anchor: Path | None = None
) -> dict[str, Any]:
    """Everything about a spec that must be equal between the real run and its replica.

    ``anchor`` is the workspace root of that spec (the replica root for the replica).
    Paths below it are normalized away; the complete spec otherwise has to match (nodes,
    dependency edges, command arguments, gates, companions, state and write paths), except
    ``created_at``. The raw spec digest differs by design and is not compared. For a replica
    spec ``real_anchor`` is the real workspace its gates were produced under (see
    :func:`_normalize_gates`).
    """

    body = spec.to_dict()
    body.pop("created_at", None)
    _normalize_gates(body, anchor, real_anchor)
    normalized = _normalize(body, _patterns((anchor, "<ROOT>")))
    node_ids = sorted(node.node_id for node in spec.nodes)
    return {
        "code_commit": spec.code_commit,
        "node_count": len(node_ids),
        "normalized_spec_sha256": hashlib.sha256(
            canonical_json_bytes(normalized)
        ).hexdigest(),
        "release_generations": dict(sorted(spec.release_generations.items())),
        "target_count": len(spec.release_generations),
    }


def _under(path: Path, bases: list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved.is_relative_to(base) for base in bases)


def is_replica_spec(spec: PipelineSpec) -> bool:
    """True only when every write-capable path is confined to one marked replica root.

    The coordination root and every release root sit under the marker's directory; the
    state root is under it or listed in the marker's ``allowed_state_roots``; every
    command's declared write is under the marker's directory or the state root. Anything
    else fails closed: such a spec needs a receipt like a real one.
    """

    anchor = _marked_ancestor(spec.coordination_root)
    if anchor is None:
        return False
    marker = read_marker(anchor) or {}
    allowed = [Path(item).resolve() for item in marker.get("allowed_state_roots", [])]
    if not all(_under(root, [anchor]) for root in spec.roots.values()):
        return False
    if not _under(spec.state_root, [anchor, *allowed]):
        return False
    write_bases = [anchor, spec.state_root.resolve()]
    for node in spec.nodes:
        for write in getattr(node, "declared_writes", ()):
            if not _under(Path(write), write_bases):
                return False
    return True


def replica_anchor(spec: PipelineSpec) -> Path:
    anchor = _marked_ancestor(spec.coordination_root)
    if anchor is None:
        raise ReplicaGateError("spec is not under a replica root marker")
    return anchor


def build_receipt(
    *,
    real_spec: PipelineSpec,
    real_anchor: Path,
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
        "real_anchor": real_anchor.as_posix(),
        "real_topology": topology(real_spec, real_anchor),
        "replica_spec_sha256": replica_spec.spec_sha256,
        "replica_topology": topology(
            replica_spec, replica_anchor(replica_spec), real_anchor
        ),
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
    recorded_anchor = receipt.get("real_anchor")
    if not isinstance(recorded_anchor, str):
        raise ReplicaGateError("replica receipt names no real workspace anchor")
    anchor = Path(recorded_anchor)
    if not all(
        _under(path, [anchor.resolve()])
        for path in (spec.coordination_root, *spec.roots.values())
    ):
        raise ReplicaGateError("receipt anchor is not the workspace of the real spec")
    wanted = topology(spec, anchor)
    if receipt.get("real_topology") != wanted:
        raise ReplicaGateError(
            "replica receipt was issued for a different real release "
            "(code commit, release generations, nodes, edges, gates, companions or "
            "paths differ after normalization)"
        )
    if receipt.get("replica_topology") != wanted:
        raise ReplicaGateError("replica ran a different topology than the real spec")
    if receipt.get("harness_code_commit") != spec.code_commit:
        raise ReplicaGateError("replica rehearsal harness commit differs from the spec")
    return receipt
