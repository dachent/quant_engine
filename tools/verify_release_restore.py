"""Clean-room restore of a coordinated release from its graph and the durable stores.

REL-PROVIDER-ROOTS-001: a release is restorable only if every generation it binds can
be reproduced, by hash, from the durable stores and the release record alone. This
tool proves it. It reads a committed release (the coordination root's
``CURRENT_RELEASE.json`` and release record) and the spec's data roots, copies ONLY the
bound generations into an empty clean-room directory, re-derives each pointer with
``publish_generation`` from the release's own identities, and requires that
``read_current_release`` then passes over the restored roots. Nothing outside the
clean-room directory is written.

Usage: ``python tools/verify_release_restore.py --spec SPEC --clean-room DIR
[--expect-sums FILE]``. ``--expect-sums`` is a ``SHA256SUMS`` file whose
``[label] <sha256> *<name>`` rows must each be found, by hash, among the restored
generation files. Exit 0 when everything verifies, 1 on any difference, 2 on usage
errors.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import shutil
import sys

from quant_engine.core import (
    GenerationNode,
    PipelineSpec,
    ReleaseAuthorityPointer,
    generation_inventory_sha256,
    publish_generation,
    read_current,
    read_release,
)


class RestoreError(Exception):
    """The release cannot be reproduced from the durable stores."""


def _sums(path: Path) -> list[tuple[str, str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        label, rest = line.split("] ", 1)
        digest, name = rest.split(" *", 1)
        rows.append((label.lstrip("["), digest, name))
    return rows


def restore(
    spec: PipelineSpec,
    clean_room: Path,
    expect_sums: Path | None = None,
    expect_labels: frozenset[str] | None = None,
) -> dict[str, object]:
    if clean_room.exists() and any(clean_room.iterdir()):
        raise RestoreError("clean-room directory must be empty")
    authority_path = spec.coordination_root / "CURRENT_RELEASE.json"
    authority = ReleaseAuthorityPointer.from_json_bytes(authority_path.read_bytes())
    manifest, journal = read_release(spec.coordination_root, authority.release_id)
    if journal.state.value != "COMMITTED":
        raise RestoreError("the authoritative release is not COMMITTED")
    clean_coordination = clean_room / "coordination"
    shutil.copytree(
        spec.coordination_root / "releases" / authority.release_id,
        clean_coordination / "releases" / authority.release_id,
    )
    shutil.copyfile(authority_path, clean_coordination / "CURRENT_RELEASE.json")
    restored_roots: dict[str, Path] = {}
    report: list[dict[str, str]] = []
    restored_files: dict[str, str] = {}
    for target in manifest.targets:
        source = (
            spec.roots[target.root_id] / "generations" / target.proposed_generation_id
        )
        if not source.is_dir():
            raise RestoreError(
                f"{target.root_id}: bound generation is absent from the durable store"
            )
        root = clean_room / "roots" / target.root_id
        shutil.copytree(source, root / "generations" / target.proposed_generation_id)
        inventory = generation_inventory_sha256(root, target.proposed_generation_id)
        if inventory != target.proposed_inventory_sha256:
            raise RestoreError(f"{target.root_id}: restored inventory hash differs")
        manifest_sha = hashlib.sha256(
            (
                root / "generations" / target.proposed_generation_id / "generation.json"
            ).read_bytes()
        ).hexdigest()
        if manifest_sha != target.proposed_manifest_sha256:
            raise RestoreError(f"{target.root_id}: restored manifest hash differs")
        pointer = publish_generation(root, target.proposed_generation_id)
        if (
            pointer.generation_id != target.proposed_generation_id
            or pointer.manifest_sha256 != target.proposed_manifest_sha256
            or pointer.artifact_inventory_sha256 != target.proposed_inventory_sha256
        ):
            raise RestoreError(f"{target.root_id}: re-derived pointer differs")
        resolved = read_current(root)
        if (
            resolved.generation_id != target.proposed_generation_id
            or resolved.manifest_sha256 != target.proposed_manifest_sha256
            or resolved.artifact_inventory_sha256 != target.proposed_inventory_sha256
        ):
            raise RestoreError(f"{target.root_id}: restored root does not resolve")
        restored_roots[target.root_id] = root
        for path in sorted(
            (root / "generations" / target.proposed_generation_id).rglob("*")
        ):
            if path.is_file():
                restored_files[hashlib.sha256(path.read_bytes()).hexdigest()] = (
                    f"{target.root_id}/{path.name}"
                )
        report.append(
            {
                "root_id": target.root_id,
                "generation_id": target.proposed_generation_id,
                "inventory_sha256": inventory,
            }
        )
    for companion in manifest.companions:
        relative = Path(*companion.relative_path.split("/"))
        source_file = spec.roots[companion.root_id] / relative
        if not source_file.is_file():
            raise RestoreError(f"companion {companion.companion_id} is absent")
        if (
            hashlib.sha256(source_file.read_bytes()).hexdigest()
            != companion.proposed_sha256
        ):
            raise RestoreError(f"companion {companion.companion_id} hash differs")
        restored_companion = clean_room / "roots" / companion.root_id / relative
        restored_companion.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, restored_companion)
    # Generations the release retains as evidence without publishing them (an
    # accepted PARTIAL provider generation) are not targets, so the release record
    # does not list them; the pipeline spec's non-target generation nodes do.
    retained: list[str] = []
    for node in spec.nodes:
        if not isinstance(node, GenerationNode) or node.release_target:
            continue
        source = spec.roots[node.root_id] / "generations" / node.generation_id
        if not source.is_dir():
            raise RestoreError(
                f"{node.root_id}: retained generation is absent from the durable store"
            )
        destination = clean_room / "retained" / node.root_id / node.generation_id
        shutil.copytree(source, destination)
        for expected in node.files:
            path = destination / expected.relative_path
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected.sha256
            ):
                raise RestoreError(
                    f"{node.root_id}: retained file {expected.relative_path} differs"
                )
            restored_files[expected.sha256] = f"{node.root_id}/{path.name}"
        retained.append(f"{node.root_id}/{node.generation_id}")
    matched: list[str] = []
    if expect_sums is not None:
        for label, digest, name in _sums(expect_sums):
            if expect_labels is not None and label not in expect_labels:
                continue
            if digest not in restored_files:
                raise RestoreError(
                    f"expected {label}/{name} ({digest[:12]}) not restored"
                )
            matched.append(f"{label}/{name} -> {restored_files[digest]}")
    return {
        "release_id": authority.release_id,
        "targets": report,
        "retained_evidence": retained,
        "sums_matched": matched,
        "read_current": "PASS",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--clean-room", type=Path, required=True)
    parser.add_argument("--expect-sums", type=Path)
    parser.add_argument(
        "--expect-label",
        action="append",
        help="check only these labels of --expect-sums (default: every row)",
    )
    args = parser.parse_args(argv)
    try:
        spec = PipelineSpec.from_json_bytes(args.spec.read_bytes())
        labels = frozenset(args.expect_label) if args.expect_label else None
        result = restore(spec, args.clean_room, args.expect_sums, labels)
    except (RestoreError, OSError, ValueError) as error:
        print(f"verify-release-restore: FAIL: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
