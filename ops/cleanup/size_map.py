"""Size map of every root quant_engine touches. Metadata only -- no file is read,
so nothing hydrates a cloud placeholder."""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOTS = {
    "work": "C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine",
    "scratch": "C:/Dev/scratch/quant-engine",
    "dev": "C:/Dev",
    "tools": "C:/Tools",
    "gitdirs": "C:/Dev/gitdirs",
}


IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C


def _is_link(p: pathlib.Path) -> bool:
    try:
        tag = getattr(os.lstat(p), "st_reparse_tag", 0)
    except OSError:
        return False
    return tag in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK)


REFUSED: list[str] = []


def _ext(p) -> str:
    """\\\\?\\-prefixed path. Without it os.walk silently stops at MAX_PATH and
    every deep generation directory is counted as zero -- which understated this
    tree by gigabytes before it was caught."""
    p = os.path.abspath(str(p))
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


def walk(root: pathlib.Path):
    """Yield (relative posix path, size) for every regular file under root."""
    prefix = _ext(root)
    for dirpath, dirnames, filenames in os.walk(prefix, onerror=REFUSED.append):
        d = pathlib.Path(dirpath)
        # Do not descend through a junction or symlink: a junctioned scratch root
        # would otherwise be counted once under each parent that points at it.
        # Directories under a OneDrive/SharePoint sync root are ALSO reparse
        # points, so this must key on the tag, not on the reparse attribute --
        # pruning on the attribute would silently drop the entire work folder.
        dirnames[:] = [x for x in dirnames if not _is_link(d / x)]
        for name in filenames:
            p = d / name
            try:
                st = p.lstat()
            except OSError:
                continue
            # dirpath carries the \\?\ prefix, so relativise against that form.
            yield str(p)[len(prefix) :].lstrip("\\/").replace("\\", "/"), st.st_size


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    out = {}
    for label, raw in ROOTS.items():
        if which != "all" and which != label:
            continue
        root = pathlib.Path(raw)
        if not root.exists():
            print(f"{label}: MISSING {root}")
            continue
        by_depth2: dict[str, list[int]] = {}
        total = 0
        count = 0
        for rel, size in walk(root):
            total += size
            count += 1
            parts = rel.split("/")
            key = (
                "/".join(parts[:2])
                if len(parts) > 2
                else (parts[0] if len(parts) > 1 else "<root files>")
            )
            b = by_depth2.setdefault(key, [0, 0])
            b[0] += size
            b[1] += 1
        print(f"\n=== {label}  {raw}")
        print(f"    {count:,} files  {total / 2**30:.2f} GB")
        for key, (size, n) in sorted(by_depth2.items(), key=lambda kv: -kv[1][0])[:40]:
            if size < 20 * 2**20:
                break
            print(f"    {size / 2**30:8.2f} GB  {n:8,}  {key}")
        out[label] = {
            "total": total,
            "files": count,
            "children": {
                k: {"bytes": v[0], "files": v[1]} for k, v in by_depth2.items()
            },
        }
    dest = pathlib.Path(sys.argv[-1]) if sys.argv[-1].endswith(".json") else None
    if dest:
        dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(f"\nwritten {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
