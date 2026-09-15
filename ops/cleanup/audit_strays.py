"""Find real junctions, dependency trees, and stray files - and REPORT refusals.

Two traps this has to avoid, both of which produced wrong answers earlier today:

* `Get-ChildItem | Where Attributes -band ReparsePoint` matches every directory
  under a OneDrive sync root, because cloud placeholders are reparse points too.
  Only the reparse TAG separates a junction from a placeholder.
* An enumeration that swallows access-denied reports a locked directory as
  empty. That is how a directory holding `epoch-manifest-source.json` was
  reported as containing zero files. Refusals are counted and printed here.
"""

from __future__ import annotations

import os
import pathlib
import sys

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C
DEP_DIRS = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    ".mypy_cache",
    ".uv-cache",
}


def ext(p) -> str:
    p = os.path.abspath(str(p))
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


def tag(p: pathlib.Path) -> int:
    try:
        return getattr(os.lstat(ext(p)), "st_reparse_tag", 0)
    except OSError:
        return 0


def main() -> int:
    root = pathlib.Path(sys.argv[1])
    junctions: list[tuple[str, str]] = []
    deps: list[tuple[str, int, int]] = []
    denied: list[str] = []
    stack = [root]
    dirs = 0

    while stack:
        cur = stack.pop()
        try:
            entries = os.listdir(ext(cur))
        except OSError as e:
            denied.append(f"WinError {e.winerror}  {cur}")
            continue
        dirs += 1
        for name in entries:
            child = cur / name
            try:
                is_dir = os.path.isdir(ext(child))
            except OSError:
                continue
            if not is_dir:
                continue
            t = tag(child)
            if t in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK):
                kind = "junction" if t == IO_REPARSE_TAG_MOUNT_POINT else "symlink"
                try:
                    target = os.readlink(ext(child))
                except OSError:
                    target = "<unreadable>"
                junctions.append((f"{kind}: {child.relative_to(root)}", target))
                continue  # never descend a link
            if name in DEP_DIRS:
                n = b = 0
                for dp, _d, fs in os.walk(ext(child), onerror=lambda e: None):
                    for f in fs:
                        try:
                            b += os.lstat(os.path.join(dp, f)).st_size
                            n += 1
                        except OSError:
                            pass
                deps.append((str(child.relative_to(root)), n, b))
                continue
            stack.append(child)

    print(f"directories walked: {dirs:,}")
    print(f"\n=== real junctions/symlinks: {len(junctions)}")
    for rel, target in junctions:
        print(f"  {rel}\n      -> {target}")
    print(f"\n=== dependency/cache trees: {len(deps)}")
    for rel, n, b in sorted(deps, key=lambda r: -r[2]):
        print(f"  {b / 2**20:9,.1f} MB {n:7,} files  {rel}")
    print(f"\n=== enumeration refused: {len(denied)}")
    for line in denied[:20]:
        print(f"  {line}")
    if len(denied) > 20:
        print(f"  ... and {len(denied) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
