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
from pathlib import Path
import sys

from quant_engine.core import read_current

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


def check_root(root: Path) -> tuple[bool, str]:
    """Return (ok, message) for one data root."""

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
            print(f"{name}: absent")
            continue
        ok, message = check_root(root)
        failed += not ok
        print(f"{name}: {'PASS' if ok else 'FAIL'} {message}")
    print(f"data-roots: {'FAIL' if failed else 'PASS'} ({failed} failing)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
