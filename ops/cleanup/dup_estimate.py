"""Duplicate estimate across the quant_engine roots, from metadata alone.

No file is opened. Reading even 64 KB of each file would hydrate 400,000 cloud
placeholders through cldflt, which is the failure mode that hard-froze this
workstation once already. So the key is (basename, size): for a tree of
content-addressed JSON written by the same producers, an identical name at an
identical byte count is a duplicate to within noise, and it is an upper bound
that can be stated honestly as one.
"""

from __future__ import annotations

import collections
import json
import os
import pathlib
import sys

ROOTS = [
    ("work", "C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"),
    ("scratch", "C:/Dev/scratch/quant-engine"),
    ("scratch_", "C:/Dev/scratch/quant_engine"),
]

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C


def _is_link(p: pathlib.Path) -> bool:
    try:
        return getattr(os.lstat(p), "st_reparse_tag", 0) in (
            IO_REPARSE_TAG_MOUNT_POINT,
            IO_REPARSE_TAG_SYMLINK,
        )
    except OSError:
        return False


def main() -> int:
    groups: dict[tuple[str, int], list[str]] = collections.defaultdict(list)
    total = 0
    count = 0

    for label, raw in ROOTS:
        root = pathlib.Path(raw)
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            d = pathlib.Path(dirpath)
            dirnames[:] = [x for x in dirnames if not _is_link(d / x)]
            for name in filenames:
                p = d / name
                try:
                    size = p.lstat().st_size
                except OSError:
                    continue
                if size == 0:
                    continue
                total += size
                count += 1
                groups[(name, size)].append(f"{label}:{p.relative_to(root).as_posix()}")

    redundant_bytes = 0
    redundant_files = 0
    worst: list[tuple[int, int, str, int]] = []
    for (name, size), paths in groups.items():
        if len(paths) < 2:
            continue
        extra = len(paths) - 1
        redundant_files += extra
        redundant_bytes += extra * size
        worst.append((extra * size, len(paths), name, size))

    print(f"total   {count:,} files  {total / 2**30:.2f} GB")
    print(
        f"copies  {redundant_files:,} files  {redundant_bytes / 2**30:.2f} GB "
        f"({redundant_bytes / total * 100:.1f}% of bytes are a repeat of a "
        f"same-name same-size file seen elsewhere)"
    )
    print(f"unique  {(total - redundant_bytes) / 2**30:.2f} GB\n")

    print("=== biggest redundancy by (name, size) ===")
    worst.sort(reverse=True)
    for waste, n, name, size in worst[:35]:
        print(f"  {waste / 2**30:7.2f} GB  x{n:5,}  {size:12,}  {name}")

    # where the copies live, rolled up to the run-tree level
    by_tree: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for (name, size), paths in groups.items():
        if len(paths) < 2:
            continue
        for p in paths[1:]:
            label, rel = p.split(":", 1)
            parts = rel.split("/")
            key = f"{label}:" + "/".join(parts[:2] if len(parts) > 2 else parts[:1])
            by_tree[key][0] += size
            by_tree[key][1] += 1
    print("\n=== redundant bytes by tree ===")
    for key, (b, n) in sorted(by_tree.items(), key=lambda kv: -kv[1][0])[:30]:
        print(f"  {b / 2**30:7.2f} GB  {n:8,}  {key}")

    dest = pathlib.Path(sys.argv[-1]) if sys.argv[-1].endswith(".json") else None
    if dest:
        dest.write_text(
            json.dumps(
                {
                    "total_bytes": total,
                    "total_files": count,
                    "redundant_bytes": redundant_bytes,
                    "redundant_files": redundant_files,
                    "by_tree": {k: {"bytes": v[0], "files": v[1]} for k, v in by_tree.items()},
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\nwritten {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
