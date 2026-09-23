"""Delete a tree that Remove-Item cannot, and say why each failure happened.

Three things defeat the ordinary deleters on this workstation:

* generation directories are marked read-only on purpose, because a generation
  is immutable;
* paths reach 370 characters against a 260 limit with LongPathsEnabled=0, and
  Windows reports that as "Access to the path ... is denied", which reads as a
  permissions problem and is not one (#419);
* directories under the sync root are dehydrated cloud placeholders that
  enumerate as empty until cldflt recalls them.

The extended-length prefix fixes the second, clearing the attribute fixes the
first, and walking bottom-up with a retry handles the third.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import sys


def ext(p: str) -> str:
    """\\\\?\\-prefixed absolute path: the only form that exceeds MAX_PATH."""
    p = os.path.abspath(p)
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


BLOCKED: list[str] = []


def _on_error(func, path, exc_info):
    """Clear the read-only bit and retry, then record and continue.

    Aborting the whole tree on the first refusal leaves 23 GB in place because
    199 directories out of 66,000 are owned by a deleted sandbox account. Those
    need an elevated shell; everything else does not, and should still go.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError as e:
        BLOCKED.append(f"[WinError {getattr(e, 'winerror', '?')}] {path}")


def tree_size(root: str) -> tuple[int, int]:
    total = files = 0
    for dirpath, _dirnames, filenames in os.walk(ext(root), onerror=lambda e: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
                files += 1
            except OSError:
                pass
    return total, files


def main() -> int:
    freed = 0
    for target in sys.argv[1:]:
        p = pathlib.Path(target)
        if not p.exists():
            print(f"skip (absent): {target}")
            continue
        size, files = tree_size(target)
        try:
            shutil.rmtree(ext(target), onerror=_on_error)
        except Exception as e:  # noqa: BLE001 - report, do not abort the batch
            print(f"ERROR {target}: {e}")
        if os.path.exists(ext(target)):
            left, leftf = tree_size(target)
            freed += size - left
            print(
                f"PARTIAL {target}: removed {(size - left) / 2**20:,.1f} MB, "
                f"{left / 2**20:,.1f} MB / {leftf:,} files still present"
            )
        else:
            freed += size
            print(f"removed {size / 2**20:11,.1f} MB  {files:8,} files  {target}")
    if BLOCKED:
        print(f"\n=== blocked: {len(BLOCKED)} paths")
        for line in BLOCKED[:8]:
            print(f"    {line}")
        if len(BLOCKED) > 8:
            print(f"    ... and {len(BLOCKED) - 8} more")
    print(f"\n=== freed {freed / 2**30:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
