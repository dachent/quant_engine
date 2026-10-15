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

``--apply`` fails closed (#541). It needs ``--reviewed-plan <plan.json>``, the
file a dry run wrote with ``--plan-out``; it deletes only when that plan's digest
equals the freshly computed one. Before any deletion it

* authenticates ``release.json`` against ``CURRENT_RELEASE.json``
  (``release_manifest_sha256``) and the journal against the release id;
* aborts on any directory-walk error, unreadable path or dangling link, since
  an incomplete census cannot authorize a delete;
* holds ``release.lock`` (the coordination mutex a release uses) and re-reads
  the committed state before every removal, so a release that moves to
  ``PREPARED`` mid-sweep stops it.
"""

from __future__ import annotations

import argparse
import hashlib
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

# ReleaseState values (core/release.py) that need no further action.
SETTLED_RELEASE_STATES = {"COMMITTED", "ROLLED_BACK"}

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C


def _is_link(p: pathlib.Path) -> bool:
    try:
        tag = getattr(os.lstat(p), "st_reparse_tag", 0)
    except OSError:
        return False
    return p.is_symlink() or tag in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK)


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


class CensusError(SystemExit):
    """The census is incomplete or the release moved; nothing may be deleted."""


def _committed_state(portfolio: pathlib.Path) -> tuple[str, bytes, dict]:
    """Authenticated committed release: (release id, snapshot, manifest).

    The snapshot is the raw authority + journal bytes; re-reading it later and
    comparing detects any release transition between selection and deletion.
    """
    coordination = portfolio / COORDINATION
    authority_bytes = (coordination / "CURRENT_RELEASE.json").read_bytes()
    authority = json.loads(authority_bytes.decode("utf-8"))
    if authority.get("state") != "COMMITTED":
        raise CensusError(
            f"release authority is {authority.get('state')}, not COMMITTED -- "
            "a release is in flight; do not sweep"
        )
    release_id = authority["release_id"]
    release_dir = coordination / "releases" / release_id
    journal_bytes = (release_dir / "journal.json").read_bytes()
    journal = json.loads(journal_bytes.decode("utf-8"))
    if journal.get("state") != "COMMITTED":
        raise CensusError(f"release journal is {journal.get('state')}, not COMMITTED")
    if journal.get("release_id") != release_id:
        raise CensusError("release journal names a different release")
    manifest_bytes = (release_dir / "release.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != authority.get(
        "release_manifest_sha256"
    ):
        raise CensusError(
            "release.json does not match the authority pointer's "
            "release_manifest_sha256; refusing to derive a keep-set from it"
        )
    # A release that was prepared but not committed leaves CURRENT_RELEASE.json on
    # the previous release while its proposed generations are not in any keep-set
    # and it holds no lock until commit. Any journal that is not settled blocks.
    outstanding = []
    for other in sorted((coordination / "releases").glob("*/journal.json")):
        state = json.loads(other.read_bytes().decode("utf-8")).get("state")
        if state not in SETTLED_RELEASE_STATES:
            outstanding.append(f"{other.parent.name[:16]}: {state}")
    if outstanding:
        raise CensusError(
            "an unsettled release exists (" + ", ".join(outstanding) + "); do not sweep"
        )
    snapshot = authority_bytes + b"\0" + journal_bytes
    return release_id, snapshot, json.loads(manifest_bytes)


def keep_set(portfolio: pathlib.Path, depth: int) -> tuple[str, dict[str, list[str]]]:
    """Generation ids that must survive, keyed by release root id."""
    release_id, _snapshot, manifest = _committed_state(portfolio)

    keep: dict[str, list[str]] = {}
    for target in manifest["targets"]:
        ids = [target["proposed_generation_id"]]
        if depth >= 2:
            previous = (target.get("previous_pointer") or {}).get("generation_id")
            if previous and previous not in ids:
                ids.append(previous)
        keep[target["root_id"]] = ids
    return release_id, keep


def _walk(root: pathlib.Path, errors: list[str]):
    """os.walk that records every error and unresolved link instead of hiding it."""

    def onerror(error: OSError) -> None:
        errors.append(f"walk error: {error}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        d = pathlib.Path(dirpath)
        for name in list(dirnames):
            if _is_link(d / name):
                dirnames.remove(name)
                if not os.path.exists(d / name):
                    errors.append(f"unresolved link: {d / name}")
        for name in filenames:
            if _is_link(d / name) and not os.path.exists(d / name):
                errors.append(f"unresolved link: {d / name}")
        yield d, dirnames, filenames


def _dir_stats(root: pathlib.Path, errors: list[str]) -> tuple[int, str]:
    """(total bytes, inventory digest) of a generation directory.

    The digest covers every file's relative path, size and mtime, so a file
    added or changed after review changes the plan and aborts the apply.
    """
    total = 0
    lines = []
    for d, _dirnames, filenames in _walk(root, errors):
        for name in filenames:
            try:
                stat_result = (d / name).lstat()
            except OSError as error:
                errors.append(f"unreadable file: {d / name}: {error}")
                continue
            total += stat_result.st_size
            relative = (d / name).relative_to(root).as_posix()
            lines.append(
                f"{relative}\t{stat_result.st_size}\t{stat_result.st_mtime_ns}"
            )
    return total, hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()


def find_generation_dirs(portfolio: pathlib.Path, errors: list[str]):
    for store in STORES:
        base = portfolio / store
        if not base.exists():
            continue
        for d, dirnames, _ in _walk(base, errors):
            if d.name in GENERATION_DIRS:
                yield d
                dirnames[:] = []  # children are generations, not more stores


def _acquire_release_lock(portfolio: pathlib.Path) -> pathlib.Path:
    """Take the coordination mutex a release holds, so none can start mid-sweep."""
    lock = portfolio / COORDINATION / "release.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise CensusError("a release holds the coordination lock; do not sweep")
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write("retention-sweep")
    return lock


def _plan(portfolio: pathlib.Path, depth: int):
    """Census every generation directory; raises CensusError if it is incomplete."""
    release_id, snapshot, _manifest = _committed_state(portfolio)
    _, keep = keep_set(portfolio, depth)
    keep_ids = {g for ids in keep.values() for g in ids}
    errors: list[str] = []
    kept: list[tuple[str, int]] = []
    dead: list[tuple[str, int, str]] = []
    for gd in find_generation_dirs(portfolio, errors):
        try:
            children = sorted(gd.iterdir())
        except OSError as error:
            errors.append(f"unreadable directory: {gd}: {error}")
            continue
        for child in children:
            rel = child.relative_to(portfolio).as_posix()
            if _is_link(child):
                errors.append(f"generation entry is a link: {rel}")
                continue
            if not child.is_dir():
                continue
            size, inventory = _dir_stats(child, errors)
            if child.name in keep_ids:
                kept.append((rel, size))
            else:
                dead.append((rel, size, inventory))
    if errors:
        raise CensusError(
            f"census incomplete ({len(errors)} problem(s)); nothing may be deleted:\n  "
            + "\n  ".join(errors[:10])
        )
    body = {
        "release_id": release_id,
        "depth": depth,
        "snapshot": hashlib.sha256(snapshot).hexdigest(),
        "dead": sorted([list(item) for item in dead]),
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return release_id, snapshot, kept, dead, {**body, "plan_digest": digest}


def sweep(
    portfolio: pathlib.Path,
    depth: int,
    apply: bool,
    plan_out: pathlib.Path | None = None,
    reviewed_plan: pathlib.Path | None = None,
) -> int:
    release_id, snapshot, kept, dead, plan = _plan(portfolio, depth)
    digest = plan["plan_digest"]
    print(f"release {release_id[:16]}  depth {depth}  plan {digest[:16]}\n")
    kept_bytes = sum(size for _, size in kept)
    dead_bytes = sum(size for _, size, _inv in dead)
    if plan_out is not None:
        plan_out.write_bytes(json.dumps(plan, indent=1, sort_keys=True).encode())
    blocked: list[tuple[str, OSError]] = []
    if apply:
        if reviewed_plan is None:
            raise CensusError("--apply requires --reviewed-plan from a dry run")
        reviewed = json.loads(reviewed_plan.read_bytes().decode("utf-8"))
        # The whole reviewed document must equal the fresh plan: matching only
        # the digest field would let an edited `dead` list ride on a valid digest.
        if reviewed != json.loads(json.dumps(plan)):
            raise CensusError(
                "reviewed plan does not match the current census; re-run the dry run"
            )
        lock = _acquire_release_lock(portfolio)
        try:
            for rel, size, inventory in dead:
                # Check-then-act: the committed state and the directory's contents
                # must be unchanged at the moment of each removal.
                _, now, _manifest = _committed_state(portfolio)
                if now != snapshot:
                    raise CensusError("the release changed during the sweep; aborted")
                recheck: list[str] = []
                _, current = _dir_stats(portfolio / rel, recheck)
                if recheck or current != inventory:
                    raise CensusError(f"{rel} changed after review; aborted")
                try:
                    shutil.rmtree(_extended(portfolio / rel), onerror=_force_remove)
                except OSError as e:
                    dead_bytes -= size
                    blocked.append((rel, e))
        finally:
            lock.unlink()

    verb = "deleted" if apply else "would delete"
    print(f"keep    {len(kept):5,} generations  {kept_bytes / 2**30:7.2f} GB")
    print(
        f"{verb:<7} {len(dead) - len(blocked):5,} generations  {dead_bytes / 2**30:7.2f} GB"
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
        print(
            "\nre-run with --apply --reviewed-plan <plan> to delete "
            "(write the plan with --plan-out)"
        )
    return 0


def demo() -> None:
    """Self-check: depth controls whether the predecessor survives."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        coordination = root / COORDINATION
        (coordination / "releases" / "rel1").mkdir(parents=True)
        (coordination / "releases" / "rel1" / "journal.json").write_text(
            json.dumps({"release_id": "rel1", "state": "COMMITTED"})
        )
        manifest = json.dumps(
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
        ).encode()
        (coordination / "releases" / "rel1" / "release.json").write_bytes(manifest)
        (coordination / "CURRENT_RELEASE.json").write_text(
            json.dumps(
                {
                    "release_id": "rel1",
                    "release_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                    "state": "COMMITTED",
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
        )  # also fails authentication: no manifest sha
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
            p.add_argument("--plan-out", type=pathlib.Path)
            p.add_argument("--reviewed-plan", type=pathlib.Path)
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
    return sweep(
        args.portfolio, args.depth, args.apply, args.plan_out, args.reviewed_plan
    )


if __name__ == "__main__":
    raise SystemExit(main())
