"""Derive the retention keep-set from the committed release, and sweep the rest.

The point of this tool is that disposition is *computed*, never recalled. A
generation survives only because the committed release manifest names it. Every
other directory under a ``generations/`` or ``candidates/`` folder is dead, and
the sweeper says so with the evidence attached.

Why the keep-set comes from the release manifest and not from ``CURRENT.json``:
``core/release.py`` re-hashes the predecessor generation of an in-flight release
on both the prepare-validation path (``_validate_prepared_state`` ->
``_validate_previous_inventory``) and the rollback path. A pointer read alone
would not name that predecessor, and deleting it would make an interrupted
commit unrecoverable. Note the coupling exists only while a release is in
flight: once the journal is ``COMMITTED``, ``_validate_committed_state`` never
reads ``previous_pointer`` again, so predecessor retention past that point is a
product decision about revertability, not a protocol requirement.

That product decision is ``--depth``:

* ``--depth 1`` keeps only the current generation of each root.
* ``--depth 2`` keeps the current generation and the one it superseded, so a
  published release can be reverted.

Depth 2 is the intended steady state. It was deliberately *not* used for the
2026-09-15 sweep, because that release's predecessor was the pre-alpha-fix
generation -- 42 of 52 hybrid alphas withheld as
``UNAVAILABLE_MODEL_RETURN_BASIS`` -- so reverting to it would have republished
a known defect. A predecessor is only worth keeping when it is a revert target
you would actually accept.

Usage::

    python retention.py keep-set  --portfolio <dir>
    python retention.py sweep     --portfolio <dir> [--depth 2] [--apply]

``sweep`` reports by default and deletes only with ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import stat
import sys

COORDINATION = "dataPortfolio/releases-refresh-2026"
STORES = [
    "dataPortfolio",
    "dataPortfolio123",
    "dataIBKR",
    "dataSchwab",
    "dataMatrixport",
    "dataQuantbase",
]
GENERATION_DIRS = {"generations", "candidates"}

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C


def _is_link(p: pathlib.Path) -> bool:
    try:
        tag = getattr(os.lstat(p), "st_reparse_tag", 0)
    except OSError:
        return False
    return tag in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK)


def _load(p: pathlib.Path):
    return json.loads(p.read_bytes().decode("utf-8"))


def _extended(p: pathlib.Path) -> str:
    """A \\\\?\\-prefixed path, the only form that survives past MAX_PATH.

    Generation paths nest a 64-hex id under several layers and reach 370
    characters here, against a 260 limit with LongPathsEnabled=0. Windows
    reports that as "Access is denied", which reads as a permissions problem
    and is not one (#419).
    """
    absolute = os.path.abspath(p)
    return absolute if absolute.startswith("\\\\?\\") else "\\\\?\\" + absolute


def _force_remove(func, path, _exc_info):
    """Clear the read-only bit and retry. Generations are written read-only."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def keep_set(portfolio: pathlib.Path, depth: int) -> tuple[str, dict[str, list[str]]]:
    """Generation ids that must survive, keyed by release root id."""
    coordination = portfolio / COORDINATION
    authority = _load(coordination / "CURRENT_RELEASE.json")
    if authority.get("state") != "COMMITTED":
        raise SystemExit(
            f"release authority is {authority.get('state')}, not COMMITTED -- "
            "a release is in flight; do not sweep"
        )
    release_id = authority["release_id"]
    release_dir = coordination / "releases" / release_id
    journal = _load(release_dir / "journal.json")
    if journal.get("state") != "COMMITTED":
        raise SystemExit(f"release journal is {journal.get('state')}, not COMMITTED")
    manifest = _load(release_dir / "release.json")

    keep: dict[str, list[str]] = {}
    for target in manifest["targets"]:
        ids = [target["proposed_generation_id"]]
        if depth >= 2:
            previous = (target.get("previous_pointer") or {}).get("generation_id")
            if previous and previous not in ids:
                ids.append(previous)
        keep[target["root_id"]] = ids
    return release_id, keep


def _dir_size(root: pathlib.Path) -> tuple[int, int]:
    total = files = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        d = pathlib.Path(dirpath)
        dirnames[:] = [x for x in dirnames if not _is_link(d / x)]
        for name in filenames:
            try:
                total += (d / name).lstat().st_size
                files += 1
            except OSError:
                pass
    return total, files


def find_generation_dirs(portfolio: pathlib.Path):
    for store in STORES:
        base = portfolio / store
        if not base.exists():
            continue
        for dirpath, dirnames, _ in os.walk(base, onerror=lambda e: None):
            d = pathlib.Path(dirpath)
            dirnames[:] = [x for x in dirnames if not _is_link(d / x)]
            if d.name in GENERATION_DIRS:
                yield d
                dirnames[:] = []  # children are generations, not more stores


def sweep(portfolio: pathlib.Path, depth: int, apply: bool) -> int:
    release_id, keep = keep_set(portfolio, depth)
    keep_ids = {g for ids in keep.values() for g in ids}
    print(
        f"release {release_id[:16]}  depth {depth}  keep-set {len(keep_ids)} generations\n"
    )

    kept = dead = 0
    kept_bytes = dead_bytes = 0
    removed: list[str] = []
    blocked: list[tuple[str, OSError]] = []
    for gd in find_generation_dirs(portfolio):
        try:
            children = sorted(c for c in gd.iterdir() if c.is_dir())
        except OSError:
            continue
        for child in children:
            size, _files = _dir_size(child)
            if child.name in keep_ids:
                kept += 1
                kept_bytes += size
                continue
            dead += 1
            dead_bytes += size
            removed.append(child.relative_to(portfolio).as_posix())
            if apply:
                try:
                    shutil.rmtree(_extended(child), onerror=_force_remove)
                except OSError as e:
                    dead_bytes -= size
                    blocked.append((child.relative_to(portfolio).as_posix(), e))

    verb = "deleted" if apply else "would delete"
    print(f"keep    {kept:5,} generations  {kept_bytes / 2**30:7.2f} GB")
    print(
        f"{verb:<7} {dead - len(blocked):5,} generations  {dead_bytes / 2**30:7.2f} GB"
    )
    if blocked:
        print(
            f"\nblocked {len(blocked):5,} generations -- reported, not silently skipped"
        )
        for rel, err in blocked[:10]:
            print(f"    [WinError {getattr(err, 'winerror', '?')}] {rel}")
        if len(blocked) > 10:
            print(f"    ... and {len(blocked) - 10} more")
        print(
            "  Access-denied here usually means a directory owned by a deleted\n"
            "  sandbox account: taking ownership needs an elevated shell."
        )
    if not apply:
        print("\nre-run with --apply to delete")
    return 0


def demo() -> None:
    """Self-check: depth controls whether the predecessor survives."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        coordination = root / COORDINATION
        (coordination / "releases" / "rel1").mkdir(parents=True)
        (coordination / "CURRENT_RELEASE.json").write_text(
            json.dumps({"release_id": "rel1", "state": "COMMITTED"})
        )
        (coordination / "releases" / "rel1" / "journal.json").write_text(
            json.dumps({"state": "COMMITTED"})
        )
        (coordination / "releases" / "rel1" / "release.json").write_text(
            json.dumps(
                {
                    "targets": [
                        {
                            "root_id": "moved",
                            "proposed_generation_id": "new",
                            "previous_pointer": {"generation_id": "old"},
                        },
                        {
                            "root_id": "unmoved",
                            "proposed_generation_id": "same",
                            "previous_pointer": {"generation_id": "same"},
                        },
                    ]
                }
            )
        )
        _, one = keep_set(root, 1)
        _, two = keep_set(root, 2)
        assert one == {"moved": ["new"], "unmoved": ["same"]}, one
        assert two == {"moved": ["new", "old"], "unmoved": ["same"]}, two

        # An in-flight release must refuse to sweep at all.
        (coordination / "CURRENT_RELEASE.json").write_text(
            json.dumps({"release_id": "rel1", "state": "PREPARED"})
        )
        try:
            keep_set(root, 1)
        except SystemExit:
            pass
        else:  # pragma: no cover
            raise AssertionError("sweep must refuse while a release is in flight")
    print("demo ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("keep-set", "sweep"):
        p = sub.add_parser(name)
        p.add_argument("--portfolio", required=True, type=pathlib.Path)
        p.add_argument("--depth", type=int, default=2, choices=(1, 2))
        if name == "sweep":
            p.add_argument("--apply", action="store_true")
    sub.add_parser("demo")

    args = parser.parse_args(argv)
    if args.command == "demo":
        demo()
        return 0
    if args.command == "keep-set":
        release_id, keep = keep_set(args.portfolio, args.depth)
        json.dump(
            {"release_id": release_id, "depth": args.depth, "keep": keep},
            sys.stdout,
            indent=1,
        )
        print()
        return 0
    return sweep(args.portfolio, args.depth, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
