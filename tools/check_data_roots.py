"""Read-only gate: every governed data root resolves its CURRENT pointer.

For each root named below that exists under ``--workspace`` this runs
``quant_engine.core.read_current`` -- the same verifier consumers use -- and
reports one line per root. A root with a ``generations/`` directory but no
``CURRENT.json`` is a failure (REL-PROVIDER-ROOTS-001): a dangling pointer, a
legacy schema-v1 pointer, a missing generation or a manifest hash difference all
make the release unreproducible. Nothing is written.

Usage: ``python tools/check_data_roots.py --workspace <dir> [--root NAME ...]``.
Exit 0 when every checked root passes, 1 otherwise, 2 on usage errors.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
from pathlib import Path
import sys

from quant_engine.core import read_current, read_generation

# Single-pointer data roots. dataPortfolio is excluded: its stores sit one level
# down and are bound by the coordinated release instead.
DATA_ROOTS = (
    "dataAlpaca",
    "dataEpochManifest",
    "dataFidelity",
    "dataIBKR",
    "dataMatrixport",
    "dataPortfolio123",
    "dataProviderTrackRecord",
    "dataQuantbase",
    "dataRobuxio",
    "dataSchwab",
    "dataStrategyBacktests",
)


# A root whose only bound generation is an accepted PARTIAL one. The provider ledger
# qualifies it ACCEPTED_PARTIAL, and a PARTIAL generation is immutable evidence, never
# current consumer authority (docs/architecture/system-boundaries.md): ``read_current``
# refuses it, correctly. Such a root passes when every retained generation is
# hash-verified; it carries no pointer a consumer may follow.
EVIDENCE_ONLY_ROOTS = {"dataIBKR": "ledger qualification ACCEPTED_PARTIAL"}


def check_evidence_root(root: Path, reason: str) -> tuple[bool, str]:
    """An evidence-only root retains verified generations and exposes no pointer.

    ``read_generation`` proves the manifest and the generation identity. A retained
    generation need not carry every file its manifest declares (source captures live
    in the provider's source-runs), so the payload check is on the bytes that ARE
    present: any declared artifact found at its id or flat basename must equal its
    declared sha256, and at least one declared artifact must be present at all.
    """

    generations = root / "generations"
    names = (
        sorted(item.name for item in generations.iterdir())
        if generations.is_dir()
        else []
    )
    if not names:
        return False, "evidence-only root retains no generation"
    for name in names:
        try:
            manifest = read_generation(root, name)
        except Exception as error:  # noqa: BLE001 - every verifier refusal is a finding
            return False, f"{name[:12]} {type(error).__name__}: {error}"
        present = 0
        for artifact in manifest.artifacts:
            base = generations / name
            for candidate in {
                base / artifact.artifact_id,
                base / Path(artifact.artifact_id).name,
            }:
                if candidate.is_file():
                    present += 1
                    actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
                    if actual != artifact.sha256:
                        return (
                            False,
                            f"{name[:12]} {artifact.artifact_id} bytes differ",
                        )
        if not present:
            return False, f"{name[:12]} retains none of its declared artifact bytes"
    pointer_note = "no CURRENT pointer"
    if (root / "CURRENT.json").exists():
        try:
            read_current(root)
        except Exception as error:  # noqa: BLE001 - an unreadable pointer is fail-closed
            pointer_note = f"CURRENT pointer is not followable ({error})"
        else:
            return False, "evidence-only root exposes a followable CURRENT pointer"
    return (
        True,
        f"evidence-only ({reason}); {len(names)} generation(s) hash-verified; "
        f"{pointer_note}",
    )


def check_root(root: Path) -> tuple[bool, str]:
    """Return (ok, message) for one data root."""

    if root.name in EVIDENCE_ONLY_ROOTS:
        # Taken whether or not generations/ exists: an evidence-only root that lost its
        # retained evidence must fail, not pass as an empty root.
        return check_evidence_root(root, EVIDENCE_ONLY_ROOTS[root.name])
    if not (root / "generations").is_dir() and not (root / "CURRENT.json").exists():
        return True, "no generations (nothing to resolve)"
    if not (root / "CURRENT.json").exists():
        return False, "generations exist but CURRENT.json is missing"
    try:
        pointer = read_current(root)
    except Exception as error:  # noqa: BLE001 - every verifier refusal is a finding
        return False, f"{type(error).__name__}: {error}"
    return True, f"CURRENT -> {pointer.generation_id[:12]}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--root", action="append", help="check only these roots")
    args = parser.parse_args(argv)
    if not args.workspace.is_dir():
        print(f"workspace is not a directory: {args.workspace}", file=sys.stderr)
        return 2
    names = tuple(args.root) if args.root else DATA_ROOTS
    unknown = [name for name in names if name not in DATA_ROOTS]
    if unknown:
        print(f"unknown root(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    failed = 0
    for name in names:
        root = args.workspace / name
        if not root.is_dir():
            # An explicitly requested root is required; a default sweep tolerates
            # roots this workspace does not have.
            if args.root:
                print(f"{name}: FAIL requested root is absent")
                failed += 1
            else:
                print(f"{name}: absent")
            continue
        ok, message = check_root(root)
        failed += not ok
        print(f"{name}: {'PASS' if ok else 'FAIL'} {message}")
    print(f"data-roots: {'FAIL' if failed else 'PASS'} ({failed} failing)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
