"""Pack a release cycle's evidence into a durable directory with a SHA256SUMS manifest.

RELEASE-DRIVER-001: the driver's state, logs and stage outputs live in a scratch cycle directory
that is deleted after the release. This tool copies the files named by ``--include`` globs
(relative to ``--cycle``) into ``--dest`` (a OneDrive evidence directory), keeps their relative
layout, and writes ``SHA256SUMS.txt`` (``<sha256> *<relative path>``) and ``manifest.json``
(counts, sizes, the driver state's code commit). It is create-only: a destination file that
already exists with other bytes is a refusal, so a second pack of a different pass cannot
overwrite the first. ``--dest`` may not lie inside ``--cycle``, and a source that is a symlink or
resolves outside the cycle is refused.

Exit 0 on success, 1 on a refusal (named on stderr), 2 on usage errors.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import shutil
import sys

STATE_NAME = "release-driver-state.json"
SUMS = "SHA256SUMS.txt"
MANIFEST = "manifest.json"


class PackError(Exception):
    """The evidence cannot be packed as asked."""


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def select(cycle: Path, includes: Sequence[str], excludes: Sequence[str]) -> list[Path]:
    found: dict[Path, None] = {}
    for pattern in includes:
        for item in sorted(cycle.glob(pattern)):
            files = (
                [p for p in sorted(item.rglob("*")) if p.is_file()]
                if item.is_dir()
                else [item]
            )
            for file in files:
                found[file] = None
    chosen = [
        file
        for file in found
        if not any(file.relative_to(cycle).match(pattern) for pattern in excludes)
    ]
    for file in chosen:
        if file.is_symlink() or cycle.resolve() not in file.resolve().parents:
            raise PackError(f"{file} is a link or resolves outside the cycle")
    if not chosen:
        raise PackError("no file matches --include")
    return chosen


def _refuse_links(dest: Path) -> None:
    """The destination and every entry already below it are real directories and files."""

    if dest.is_symlink():
        raise PackError(f"{dest} is a link")
    if dest.is_dir():
        for item in dest.rglob("*"):
            if item.is_symlink():
                raise PackError(f"{item} in the destination is a link")


def pack(
    cycle: Path, dest: Path, includes: Sequence[str], excludes: Sequence[str] = ()
) -> dict[str, object]:
    if dest.is_symlink():
        raise PackError(f"{dest} is a link")
    cycle, dest = cycle.resolve(), dest.resolve()
    if dest == cycle or cycle in dest.parents:
        raise PackError("--dest lies inside --cycle")
    files = select(cycle, includes, excludes)
    _refuse_links(dest)
    targets = {file: dest / file.relative_to(cycle) for file in files}
    for file, target in targets.items():
        if dest not in target.resolve().parents:
            raise PackError(f"{target} resolves outside {dest}")
        if target.exists() and _sha(target) != _sha(file):
            raise PackError(
                f"{target} already exists with other bytes; evidence is create-only"
            )
    rows = [
        (file.relative_to(cycle).as_posix(), _sha(file), file.stat().st_size)
        for file in files
    ]
    state_file = cycle / STATE_NAME
    state = (
        json.loads(state_file.read_bytes()) if state_file.is_file() else {"stages": {}}
    )
    stages = state.get("stages", {})
    manifest = {
        "files": len(rows),
        "bytes": sum(size for _, _, size in rows),
        "code_commits": sorted(
            {
                record.get("code_commit")
                for record in stages.values()
                if record.get("code_commit")
            }
        ),
        "stages": {name: record.get("status") for name, record in stages.items()},
    }
    metadata = {
        dest / SUMS: "".join(f"{sha} *{name}\n" for name, sha, _ in rows).encode(),
        dest / MANIFEST: (
            json.dumps(manifest, indent=1, sort_keys=True) + "\n"
        ).encode(),
    }
    for path, content in metadata.items():
        if path.exists() and path.read_bytes() != content:
            raise PackError(
                f"{path} already exists with other content; evidence is create-only"
            )
    if dest.is_dir():
        expected = set(targets.values()) | set(metadata)
        extra = sorted(
            str(p) for p in dest.rglob("*") if p.is_file() and p not in expected
        )
        if extra:
            raise PackError(f"{dest} holds files this pack does not: {extra[:3]}")
    for file, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(file, target)
    for path, content in metadata.items():
        if not path.exists():
            path.write_bytes(content)
    return manifest


def verify(dest: Path) -> list[str]:
    """Problems found re-hashing a packed directory against its SHA256SUMS.txt."""

    problems = []
    for line in (dest / SUMS).read_text(encoding="utf-8").splitlines():
        sha, name = line.split(" *", 1)
        target = dest / name
        if not target.is_file():
            problems.append(f"missing {name}")
        elif _sha(target) != sha:
            problems.append(f"changed {name}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cycle", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument(
        "--include", nargs="+", required=True, help="globs relative to --cycle"
    )
    parser.add_argument(
        "--exclude", nargs="*", default=[], help="path patterns relative to --cycle"
    )
    args = parser.parse_args(argv)
    try:
        manifest = pack(args.cycle, args.dest, args.include, args.exclude)
        problems = verify(args.dest.resolve())
    except (PackError, OSError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    if problems:
        print("verify failed: " + "; ".join(problems[:5]), file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
