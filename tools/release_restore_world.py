"""Restore the input world a release replay needs from the retained archives, by path and hash.

RELEASE-DRIVER-001: an R1/R2 replay names inputs by absolute paths of the machine that built the
original release (an old worktree, a scratch chain). Those paths no longer exist; the bytes live in
OneDrive evidence archives. This tool reads every absolute path (and every ``{path, sha256}`` pin)
in the given release specs, restores the files that fall under a configured old root from the
archives into a scratch directory, verifies each restored file against its pin, and writes a
resolution report. It never writes outside ``--dest`` and ``--report``.

A path is resolved, in order, from: the live workspace (``workspace_fallback`` prefixes; no copy),
an exact archive entry, or an archive subtree when the path names a directory. Anything else is
``MISSING``; a pinned file whose restored bytes hash differently is ``MISMATCH``. Exit 0 only when
nothing is MISMATCH and every MISSING path starts with an ``--allow-missing`` prefix (inputs the
cycle regenerates), 1 otherwise, 2 on usage errors.

World file (``ops/mvp2-release/replay-world.json``): ``roots`` lists, per old root, the ``old``
path prefix, a ``key`` (the folder under ``--dest``), the ``archive`` (relative to the archive
root), an optional ``archive_prefix`` inside the zip, optional ``only_prefixes`` (relative paths
the archive may serve) and ``workspace_fallback`` (true: a path that exists in the live workspace
is used in place, not copied).
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import sys
from typing import Any
import zipfile

_ABSOLUTE = re.compile(r"^[A-Za-z]:\\")


def _walk(value: Any, paths: set[str], pins: dict[str, str]) -> None:
    if isinstance(value, dict):
        sha, path = value.get("sha256"), value.get("path")
        if isinstance(sha, str) and len(sha) == 64 and isinstance(path, str):
            pins[path] = sha
        for item in value.values():
            _walk(item, paths, pins)
    elif isinstance(value, list):
        for item in value:
            _walk(item, paths, pins)
    elif isinstance(value, str) and _ABSOLUTE.match(value):
        paths.add(value)


def collect(spec_files: Sequence[Path]) -> tuple[set[str], dict[str, str]]:
    paths: set[str] = set()
    pins: dict[str, str] = {}
    for spec in spec_files:
        _walk(json.loads(spec.read_bytes()), paths, pins)
    return paths | set(pins), pins


def _under(path: str, old: str) -> str | None:
    """The part of ``path`` below the old root ``old`` (None when it is not below it)."""

    if path.lower().startswith(old.lower()):
        return path[len(old) :]
    if path.lower() + "\\" == old.lower():
        return ""
    return None


def _local(root_key: str, dest: Path, relative: str) -> Path:
    return dest / root_key / Path(*PureWindowsPath(relative).parts)


def resolve(
    paths: set[str],
    pins: Mapping[str, str],
    world: Mapping[str, Any],
    workspace: Path,
    dest: Path,
    archive_root: Path,
) -> list[dict[str, str]]:
    archives: dict[str, tuple[zipfile.ZipFile, set[str]]] = {}
    rows: list[dict[str, str]] = []
    restored_dirs: list[str] = []
    for path in sorted(paths):
        root = next(
            (r for r in world["roots"] if _under(path, r["old"]) is not None), None
        )
        if root is None or any(
            path.lower().startswith(d.lower() + "\\") for d in restored_dirs
        ):
            continue
        relative = str(_under(path, root["old"]))
        slashed = relative.replace("\\", "/")
        row = {"path": path, "status": "MISSING", "new": "", "detail": ""}
        rows.append(row)
        if root.get("workspace_fallback") and (workspace / slashed).exists():
            row.update(status="WORKSPACE", new=str(workspace / slashed))
            continue
        only = root.get("only_prefixes") or []
        if only and not slashed.startswith(tuple(only)):
            row["detail"] = "outside the archive's prefixes"
            continue
        archive_file = archive_root / root["archive"]
        if root["archive"] not in archives:
            if not archive_file.is_file():
                row["detail"] = f"archive {root['archive']} absent"
                continue
            handle = zipfile.ZipFile(archive_file)
            archives[root["archive"]] = (
                handle,
                {n for n in handle.namelist() if not n.endswith("/")},
            )
        archive, names = archives[root["archive"]]
        inner = root.get("archive_prefix", "")
        entry = inner + slashed
        target = _local(root["key"], dest, relative)
        if entry in names:
            data = archive.read(entry)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            row.update(status="RESTORED", new=str(target))
            if path in pins and hashlib.sha256(data).hexdigest() != pins[path]:
                row.update(
                    status="MISMATCH",
                    detail=f"archive bytes differ from the pin {pins[path][:12]}",
                )
            continue
        children = sorted(n for n in names if n.startswith(entry.rstrip("/") + "/"))
        if children:
            for name in children:
                child = _local(root["key"], dest, name[len(inner) :].replace("/", "\\"))
                child.parent.mkdir(parents=True, exist_ok=True)
                child.write_bytes(archive.read(name))
            row.update(
                status="RESTORED", new=str(target), detail=f"{len(children)} files"
            )
            restored_dirs.append(path)
    return rows


def remap_path(path: str, world: Mapping[str, Any], dest: Path) -> str:
    """The restored location of an old absolute path (unchanged when no root claims it)."""

    for root in world["roots"]:
        relative = _under(path, root["old"])
        if relative is not None:
            return str(_local(root["key"], dest, relative))
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--spec", type=Path, nargs="+", required=True)
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="the live _quant_engine directory (read only)",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        help="base of the archive paths (default: --workspace)",
    )
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--allow-missing",
        nargs="*",
        default=[],
        help="old-path prefixes the cycle regenerates",
    )
    args = parser.parse_args(argv)
    try:
        world = json.loads(args.world.read_bytes())
        paths, pins = collect(args.spec)
        rows = resolve(
            paths,
            pins,
            world,
            args.workspace,
            args.dest,
            args.archive_root or args.workspace,
        )
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    allowed = tuple(prefix.lower() for prefix in args.allow_missing)
    unexplained = [
        r
        for r in rows
        if r["status"] == "MISSING" and not r["path"].lower().startswith(allowed)
    ]
    bad = [r for r in rows if r["status"] == "MISMATCH"]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    report = {
        "counts": counts,
        "rows": rows,
        "unexplained_missing": [r["path"] for r in unexplained],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes(
        (json.dumps(report, indent=1, sort_keys=True) + "\n").encode()
    )
    print(json.dumps(counts, sort_keys=True))
    for row in bad + unexplained:
        print(f"{row['status']} {row['path']} {row['detail']}", file=sys.stderr)
    return 1 if bad or unexplained else 0


if __name__ == "__main__":
    raise SystemExit(main())
