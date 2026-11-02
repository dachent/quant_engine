"""Leaf diff of a proposed release against the previous committed release, with an explainer.

The data-only gate profile (``gate_profile``) may skip the replica ceremony and the manual
four-family review only when every change in the proposed release is explained by a changed
input. This module decides that, in bytes, per release target:

* a target whose generation id did not change contributes nothing;
* a target whose generation id changed is explainable only if it is a source (no
  ``depends_on`` at all: its own new bytes are the input change) or at least one release
  target among its transitive ``depends_on`` ancestors changed generation id. A derived target
  that changed while no ancestor changed is the signature of a code change, nondeterminism or a
  wrong join, and is unexplained;
* inside a changed target only files whose sha256 differs are read. A JSON file is compared leaf
  by leaf. A changed number, a changed 32-128 hex id, a list length change, or a string that
  differs only in its digit runs is explainable by the ancestor rule. An object key added or
  removed, a type change, a boolean flip, a string whose text differs beyond its digits (wording
  or a name), an added or removed file, an unreadable or oversized JSON file, or new bytes that do
  not equal the spec's expectation are never explainable.

The explanation is lineage-coarse: one changed ancestor explains every downstream change. It
proves "this moved because an input moved", not "the result is right".
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Final, Mapping

from .pipeline import GenerationNode, PipelineSpec

MAX_JSON_BYTES: Final = 256 * 1024 * 1024
_SAMPLES_PER_TARGET: Final = 40
_DIGITS: Final = re.compile(r"\d+")
_HEX_ID: Final = re.compile(r"[0-9a-f]{32,128}")

# Kinds an input change can explain, and kinds nothing can explain.
EXPLAINABLE: Final = frozenset({"number", "id", "text_digits", "length", "opaque_file"})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare(old: Any, new: Any, path: str, out: list[tuple[str, str]]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        if old.keys() != new.keys():
            added = sorted(set(new) - set(old))[:3]
            removed = sorted(set(old) - set(new))[:3]
            out.append(("key_set", f"{path} +{added} -{removed}"))
        for key in sorted(old.keys() & new.keys()):
            _compare(old[key], new[key], f"{path}/{key}", out)
        return
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            out.append(("length", f"{path} {len(old)}->{len(new)}"))
        for index, (a, b) in enumerate(zip(old, new)):
            _compare(a, b, f"{path}/{index}", out)
        return
    if old == new and type(old) is type(new):
        return
    if _is_number(old) and _is_number(new):
        if old != new:
            out.append(("number", path))
        return
    if isinstance(old, str) and isinstance(new, str):
        if _HEX_ID.fullmatch(old) and _HEX_ID.fullmatch(new):
            out.append(("id", path))
        elif _DIGITS.sub("#", old) == _DIGITS.sub("#", new):
            out.append(("text_digits", path))
        else:
            out.append(("wording", path))
        return
    if isinstance(old, bool) or isinstance(new, bool):
        out.append(("boolean_changed", path))
        return
    out.append(("type", f"{path} {type(old).__name__}->{type(new).__name__}"))


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("not a regular file")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("oversized")
    return json.loads(path.read_bytes().decode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _previous_files(directory: Path) -> dict[str, str] | None:
    try:
        inventory = json.loads(
            (directory / "generation-inventory.json").read_bytes().decode("utf-8")
        )
        return {
            str(item["relative_path"]): str(item["sha256"])
            for item in inventory["files"]
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _ancestors(spec: PipelineSpec, node_id: str) -> set[str]:
    by_id = {node.node_id: node for node in spec.nodes}
    seen: set[str] = set()
    stack = list(by_id[node_id].depends_on)
    while stack:
        current = stack.pop()
        if current in seen or current not in by_id:
            continue
        seen.add(current)
        stack.extend(by_id[current].depends_on)
    return seen


def explain_release(
    spec: PipelineSpec,
    previous: Mapping[str, tuple[str, Path]],
    roots: Mapping[str, Path],
) -> dict[str, Any]:
    """Report for ``spec`` against ``previous`` (``root_id -> (generation_id, generation_dir)``).

    ``unexplained`` is empty only when every change is explainable (see the module docstring);
    ``explained`` is then true. The report names every changed target, its changed ancestors, the
    changed files and a bounded sample of changed leaves."""

    targets = {
        node.root_id: node
        for node in spec.nodes
        if isinstance(node, GenerationNode) and node.release_target
    }
    nodes_by_id = {node.node_id: node for node in spec.nodes}
    changed = {
        root
        for root, node in targets.items()
        if root not in previous or previous[root][0] != node.generation_id
    }
    unexplained: list[str] = []
    for root in sorted(set(previous) - set(targets)):
        unexplained.append(f"{root}: target removed from the release")
    report_targets: list[dict[str, Any]] = []
    for root in sorted(changed):
        node = targets[root]
        entry: dict[str, Any] = {
            "root_id": root,
            "generation_id": node.generation_id,
            "previous_generation_id": previous[root][0] if root in previous else None,
        }
        report_targets.append(entry)
        if root not in previous:
            unexplained.append(f"{root}: target is new, no previous generation to diff")
            entry["counts"] = {}
            continue
        ancestor_ids = _ancestors(spec, node.node_id)
        gen_ancestors = [
            nodes_by_id[item]
            for item in ancestor_ids
            if isinstance(nodes_by_id[item], GenerationNode)
        ]
        changed_ancestors = sorted(
            {
                a.root_id
                for a in gen_ancestors
                if isinstance(a, GenerationNode)
                and a.release_target
                and a.root_id in changed
                and a.root_id != root
            }
        )
        is_source = not node.depends_on
        entry["changed_ancestors"] = changed_ancestors
        entry["source"] = is_source
        explainable = is_source or bool(changed_ancestors)
        if not explainable:
            unexplained.append(
                f"{root}: generation changed but no input ancestor changed"
            )
        found: list[tuple[str, str]] = []
        old_files = _previous_files(previous[root][1])
        new_files = {item.relative_path: item.sha256 for item in node.files}
        if old_files is None:
            unexplained.append(f"{root}: previous generation inventory unreadable")
            entry["counts"] = {}
            continue
        for rel in sorted(set(old_files) ^ set(new_files)):
            found.append(("file_set", rel))
        new_dir = node.source_dir
        if new_dir is None or not new_dir.is_dir():
            fallback = roots.get(root)
            new_dir = (
                fallback / "generations" / node.generation_id
                if fallback is not None
                else None
            )
        files_changed: list[str] = []
        for rel in sorted(set(old_files) & set(new_files)):
            if old_files[rel] == new_files[rel]:
                continue
            files_changed.append(rel)
            if not rel.endswith(".json"):
                found.append(("opaque_file", rel))
                continue
            try:
                if new_dir is None:
                    raise ValueError("new bytes unavailable")
                new_path = new_dir / rel
                if _sha256_file(new_path) != new_files[rel]:
                    raise ValueError("new bytes differ from the spec expectation")
                _compare(
                    _read_json(previous[root][1] / rel),
                    _read_json(new_path),
                    rel,
                    found,
                )
            except (OSError, ValueError) as exc:
                found.append(("unreadable", f"{rel}: {exc}"))
        counts: dict[str, int] = {}
        for kind, _detail in found:
            counts[kind] = counts.get(kind, 0) + 1
        entry["counts"] = dict(sorted(counts.items()))
        entry["files_changed"] = files_changed
        entry["samples"] = [
            {"kind": kind, "detail": detail}
            for kind, detail in found[:_SAMPLES_PER_TARGET]
        ]
        for kind, detail in found:
            if kind not in EXPLAINABLE:
                unexplained.append(f"{root}: {kind}: {detail}")
    return {
        "explained": not unexplained,
        "kind": "quant-engine-release-leaf-diff",
        "schema_version": 1,
        "targets": report_targets,
        "unchanged_targets": sorted(set(targets) - changed),
        "unexplained": unexplained[:200],
        "unexplained_count": len(unexplained),
    }
