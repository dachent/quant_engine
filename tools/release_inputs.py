"""Move cutoff-dependent tracked inputs out of Git and verify them (``core.release_inputs``).

``census``   list tracked files by registry disposition (release-input / code-bound / policy).
``extract``  copy every tracked release-input blob of a commit into a NEW root outside the
             repository and write its manifest; prints the manifest sha256 (the pin).
``verify``   load a root against a pin, hashing every file; exit 0 only if all hold.
``path``     print the verified absolute path of one repo-relative input (for shell drivers).
``add``      build the NEXT root from a verified one with extra files: each ``--file LOGICAL=SOURCE`` copies
             a source file byte for byte to a registered release-input path of the new root (a path the
             root already holds is refused without ``--replace``); prints the new manifest sha256 (the pin).
``new-cutoff``  build a NEW root at a stated cutoff from a verified source root plus explicit
             ``--file ENTRY=PATH`` replacements/additions and ``--drop ENTRY``; refuses to seal while a
             cutoff-dependent entry (``CUTOFF_DEPENDENT`` in ``core.release_inputs``) is carried unreplaced
             unless ``--carry-stale ENTRY=REASON`` names it; create-only output, loader-verified, deterministic.
``rebind-predecessor``  build the NEXT root from a verified one: its disclosure inputs'
             ``predecessor_comparison`` is derived from the committed ``CURRENT_RELEASE`` and its
             panel's ``dashboard_data.json`` (never typed); prints the new manifest sha256 (the pin).

The root and the pin are arguments; nothing is hardcoded. A missing, changed or unpinned
input is a refusal (exit 1), never a fallback to a repo copy.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

import sleeve_disclosures

from quant_engine.core import (
    RELEASE_INPUTS_CODE_BOUND as CODE_BOUND,
    RELEASE_INPUTS_KIND as RELEASE_INPUT,
    RELEASE_INPUTS_POLICY as POLICY,
    ReleaseInputs,
    ReleaseInputsError,
    is_cutoff_dependent_release_input as is_cutoff_dependent,
    build_release_inputs_manifest as build_manifest,
    classify_release_input as classify,
    load_release_inputs,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DISCLOSURE_INPUTS = "ops/mvp2-release/inputs/sleeves/disclosure-inputs.json"
POLICY_MANIFEST = "ops/mvp2-release/inputs/sleeves/sleeve-policy-manifest.json"
POLICY_DISCLOSURE_KEY = "disclosure-inputs"
PANEL_DATA_RELATIVE = (
    "dataPortfolio/control-panel/generations/{panel}/data/dashboard_data.json"
)


def _git(repo: Path, *args: str) -> bytes:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if done.returncode != 0:
        raise ReleaseInputsError(
            f"git {' '.join(args)} failed: {done.stderr.decode(errors='replace').strip()}"
        )
    return done.stdout


def _tracked(repo: Path, commit: str) -> list[str]:
    raw = _git(repo, "ls-tree", "-r", "--name-only", "-z", commit)
    return [name for name in raw.decode("utf-8").split("\0") if name]


def _census(args: argparse.Namespace) -> int:
    groups: dict[str, list[str]] = {RELEASE_INPUT: [], CODE_BOUND: [], POLICY: []}
    for name in _tracked(args.repo_root, args.commit):
        disposition = classify(name)
        if disposition is not None:
            groups[disposition].append(name)
    print(json.dumps({key: sorted(value) for key, value in groups.items()}, indent=1))
    return 0


def _extract(args: argparse.Namespace) -> int:
    out = args.out_root
    if out.exists() and any(out.iterdir()):
        raise ReleaseInputsError(f"extract target is not empty: {out}")
    inside = args.repo_root.resolve()
    if out.resolve() == inside or inside in out.resolve().parents:
        raise ReleaseInputsError(
            "the release-inputs root must be outside the repository"
        )
    names = [
        name
        for name in _tracked(args.repo_root, args.commit)
        if classify(name) == RELEASE_INPUT
    ]
    for name in names:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            _git(args.repo_root, "cat-file", "blob", f"{args.commit}:{name}")
        )
    _, pin = build_manifest(out, names, cutoff=args.cutoff)
    print(json.dumps({"entries": len(names), "manifest_sha256": pin}))
    return 0


def _load(args: argparse.Namespace) -> ReleaseInputs:
    loaded = load_release_inputs(args.root, args.manifest_sha256)
    if args.cutoff is not None and loaded.cutoff != args.cutoff:
        raise ReleaseInputsError(
            f"release inputs are for cutoff {loaded.cutoff}, not {args.cutoff}"
        )
    return loaded


def _verify(args: argparse.Namespace) -> int:
    loaded = _load(args)
    print(
        json.dumps(
            {
                "cutoff": loaded.cutoff,
                "entries": len(loaded.paths),
                "manifest_sha256": loaded.manifest_sha256,
            }
        )
    )
    return 0


def _path(args: argparse.Namespace) -> int:
    loaded = _load(args)
    print(loaded.path(args.logical))
    return 0


def _canonical_path(path: Path) -> str:
    """Links resolved (also for a path that does not exist yet) and the case Windows ignores folded."""

    return os.path.normcase(os.path.realpath(path))


def _nested(path: Path, base: Path) -> bool:
    """``path`` is ``base`` or lies below it, on canonical spellings."""

    inner, outer = _canonical_path(path), _canonical_path(base)
    return inner == outer or inner.startswith(outer.rstrip(os.sep) + os.sep)


def _canonical(value: Any) -> bytes:
    """The formatting of the committed disclosure files: indent 2, sorted keys, final newline."""

    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def _read_canonical(path: Path, what: str) -> dict[str, Any]:
    """The object, refused unless re-serializing it reproduces the file (only the rebound fields change)."""

    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise ReleaseInputsError(
            f"{what} is not in the canonical formatting: not rewritten"
        )
    return value


def _derived_predecessor(data_root: Path, old: dict[str, Any]) -> dict[str, Any]:
    """The predecessor comparison of the committed release: ids and data file from committed bytes.

    ``pointers`` (where in the dashboard data the figures are read) is the one authored part and is kept.
    """

    try:
        release_id, panel = sleeve_disclosures.committed_release_panel(data_root)
    except sleeve_disclosures.DisclosureError as exc:
        raise ReleaseInputsError(f"the committed release is unreadable: {exc}") from exc
    relative = PANEL_DATA_RELATIVE.format(panel=panel)
    data_file = data_root.joinpath(*relative.split("/"))
    if not data_file.is_file():
        raise ReleaseInputsError(
            f"the committed panel has no dashboard data: {data_file}"
        )
    return {
        **{key: value for key, value in old.items() if key == "pointers"},
        "release_id": release_id,
        "panel_generation_id": panel,
        "data_file": {
            "relative_path": relative,
            "sha256": hashlib.sha256(data_file.read_bytes()).hexdigest(),
        },
    }


def _stale_references(
    root: Path, skip: set[str], needles: dict[str, bytes]
) -> list[str]:
    hits = []
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root).as_posix()
        if not item.is_file() or relative in skip:
            continue
        raw = item.read_bytes()
        names = [label for label, needle in needles.items() if needle in raw]
        if names:
            hits.append(f"{relative}: {', '.join(names)}")
    return hits


def _checked_logical(logical: str) -> str:
    """A safe relative path the registry names as a release input, or a refusal."""

    # the registry patterns cross "/", so a path with ".." would still classify
    if (
        logical.startswith("/")
        or "\\" in logical
        or ":" in logical
        or any(part in ("", ".", "..") for part in logical.split("/"))
    ):
        raise ReleaseInputsError(
            f"release input path is not a safe relative path: {logical!r}"
        )
    if classify(logical) != RELEASE_INPUT:
        raise ReleaseInputsError(f"{logical} is not a release input in the registry")
    return logical


def _add(args: argparse.Namespace) -> int:
    loaded = _load(args)
    additions: dict[str, Path] = {}
    for item in args.file:
        logical, separator, source = item.partition("=")
        if not separator or not logical or not source:
            raise ReleaseInputsError(f"--file wants LOGICAL=SOURCE, got {item!r}")
        _checked_logical(logical)
        if logical in additions:
            raise ReleaseInputsError(f"{logical} is named twice")
        if logical in loaded.paths and not args.replace:
            raise ReleaseInputsError(
                f"{logical} is already in the root (pass --replace to swap its bytes)"
            )
        path = Path(source)
        if path.is_symlink() or not path.is_file():
            raise ReleaseInputsError(f"{source} is not a regular file")
        additions[logical] = path
    if not additions:
        raise ReleaseInputsError("nothing to add: pass --file LOGICAL=SOURCE")
    out = args.out
    if _nested(out, loaded.root) or _nested(loaded.root, out):
        raise ReleaseInputsError(
            "the new root must be a separate directory: it is, lies inside or contains the source root"
        )
    if out.exists() and any(out.iterdir()):
        raise ReleaseInputsError(f"target is not empty: {out}")
    inside = REPO_ROOT.resolve()
    if out.resolve() == inside or inside in out.resolve().parents:
        raise ReleaseInputsError(
            "the release-inputs root must be outside the repository"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{out.name}.building-", dir=out.parent))
    try:
        shutil.copytree(loaded.root, staging, symlinks=False, dirs_exist_ok=True)
        (staging / "release-inputs.manifest.json").unlink()
        for logical, source in additions.items():
            target = staging.joinpath(*logical.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        paths = sorted({*loaded.paths, *additions})
        _, pin = build_manifest(staging, paths, cutoff=loaded.cutoff)
        added = load_release_inputs(staging, pin)
        for logical, source in additions.items():
            if added.sha256(logical) != hashlib.sha256(source.read_bytes()).hexdigest():
                raise ReleaseInputsError(
                    f"{logical} differs from its source (the source changed while it was copied)"
                )
        if out.exists():
            out.rmdir()  # empty (checked above)
        os.replace(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # our own staging directory
        raise
    print(
        json.dumps(
            {
                "status": "added",
                "added": {
                    logical: added.sha256(logical) for logical in sorted(additions)
                },
                "manifest_sha256": pin,
            }
        )
    )
    return 0


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BASELINE_ENTRY = "acceptance/expected-baseline.json"


def _strict_date(value: str) -> str:
    if not _ISO_DATE.fullmatch(value):
        raise ReleaseInputsError(f"--cutoff is not a YYYY-MM-DD date: {value!r}")
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ReleaseInputsError(f"--cutoff is not a real date: {value!r}") from exc
    return value


def _pairs(items: Sequence[str], flag: str, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        left, separator, right = item.partition("=")
        if not separator or not left or not right:
            raise ReleaseInputsError(f"{flag} wants {what}, got {item!r}")
        _checked_logical(left)
        if left in out:
            raise ReleaseInputsError(f"{left} is named twice in {flag}")
        out[left] = right
    return out


def _new_cutoff(args: argparse.Namespace) -> int:
    cutoff = _strict_date(args.cutoff)
    loaded = load_release_inputs(args.root, args.manifest_sha256)
    files = _pairs(args.file, "--file", "ENTRY=PATH")
    if args.baseline is not None:
        if _BASELINE_ENTRY in files:
            raise ReleaseInputsError(
                f"{_BASELINE_ENTRY} is named twice (--baseline and --file)"
            )
        files[_BASELINE_ENTRY] = str(args.baseline)
    stale = _pairs(args.carry_stale, "--carry-stale", "ENTRY=REASON")
    drops: set[str] = set()
    for name in args.drop:
        _checked_logical(name)
        if name in drops:
            raise ReleaseInputsError(f"{name} is named twice in --drop")
        if name not in loaded.paths:
            raise ReleaseInputsError(f"--drop {name}: the source root does not hold it")
        drops.add(name)
    sources: dict[str, Path] = {}
    for logical, source in files.items():
        path = Path(source)
        if path.is_symlink() or not path.is_file():
            raise ReleaseInputsError(f"{source} is not a regular file")
        if logical in drops:
            raise ReleaseInputsError(f"{logical} is both replaced and dropped")
        sources[logical] = path
        if fnmatch.fnmatchcase(logical, "acceptance/expected-baseline*.json"):
            try:
                header = json.loads(path.read_bytes()).get("cutoff")
            except (ValueError, AttributeError) as exc:
                raise ReleaseInputsError(f"{source} is not a JSON baseline") from exc
            if header != cutoff:
                raise ReleaseInputsError(
                    f"baseline {source} declares cutoff {header!r}, not {cutoff}"
                )
    carry: dict[str, str] = {}
    for pattern, reason in stale.items():
        if not reason.strip():
            raise ReleaseInputsError(f"--carry-stale {pattern}: the reason is empty")
        wild = any(char in pattern for char in "*?[")
        hits = [name for name in loaded.paths if fnmatch.fnmatchcase(name, pattern)]
        if not hits:
            raise ReleaseInputsError(
                f"--carry-stale {pattern}: the source root holds no such entry"
            )
        for name in hits:
            if name in drops or name in sources:
                if wild:
                    continue
                raise ReleaseInputsError(
                    f"--carry-stale {name}: it is replaced or dropped, so it is not carried"
                )
            if not is_cutoff_dependent(name):
                if wild:
                    continue
                raise ReleaseInputsError(
                    f"--carry-stale {name}: it is not cutoff-dependent"
                )
            carry.setdefault(name, reason)
    stale = carry
    carried = [
        name for name in loaded.paths if name not in drops and name not in sources
    ]
    carried_dependent = [name for name in carried if is_cutoff_dependent(name)]
    if cutoff != loaded.cutoff:
        unresolved = [name for name in carried_dependent if name not in stale]
        if unresolved:
            raise ReleaseInputsError(
                f"cutoff-dependent entries of the {loaded.cutoff} root would be carried unchanged "
                f"into {cutoff}: replace them (--file ENTRY=PATH), drop them (--drop ENTRY) or "
                "name them (--carry-stale ENTRY=REASON):\n  CARRIED_UNCHANGED "
                + "\n  CARRIED_UNCHANGED ".join(unresolved)
            )
    out = args.out
    if _nested(out, loaded.root) or _nested(loaded.root, out):
        raise ReleaseInputsError(
            "the new root must be a separate directory: it is, lies inside or contains the source root"
        )
    if os.path.lexists(out):
        raise ReleaseInputsError(f"target exists (create-only): {out}")
    inside = REPO_ROOT.resolve()
    if out.resolve() == inside or inside in out.resolve().parents:
        raise ReleaseInputsError(
            "the release-inputs root must be outside the repository"
        )
    paths = sorted({*carried, *sources})
    if not paths:
        raise ReleaseInputsError("the new root would hold no entries")
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{out.name}.building-", dir=out.parent))
    try:
        for name in paths:
            target = staging.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                sources[name] if name in sources else loaded.path(name), target
            )
        _, pin = build_manifest(staging, paths, cutoff=cutoff)
        built = load_release_inputs(staging, pin)
        if built.cutoff != cutoff or built.paths != tuple(paths):
            raise ReleaseInputsError("the sealed root does not match the request")
        for name in paths:
            want = (
                hashlib.sha256(sources[name].read_bytes()).hexdigest()
                if name in sources
                else loaded.sha256(name)
            )
            if built.sha256(name) != want:
                raise ReleaseInputsError(
                    f"{name} differs from its source (the source changed while it was copied)"
                )
        os.replace(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # our own staging directory
        raise
    print(
        json.dumps(
            {
                "status": "new_cutoff",
                "cutoff": cutoff,
                "source_cutoff": loaded.cutoff,
                "source_manifest_sha256": loaded.manifest_sha256,
                "entries": len(paths),
                "replaced": {
                    n: built.sha256(n) for n in sorted(sources) if n in loaded.paths
                },
                "added": {
                    n: built.sha256(n) for n in sorted(sources) if n not in loaded.paths
                },
                "dropped": sorted(drops),
                "carried_unchanged": {
                    n: stale.get(n, "") for n in sorted(carried_dependent)
                },
                "warnings": [
                    f"CARRIED_UNCHANGED {n}: {stale.get(n) or 'same cutoff'}"
                    for n in sorted(carried_dependent)
                ],
                "manifest_sha256": pin,
            },
            indent=1,
        )
    )
    return 0


def _rebind_predecessor(args: argparse.Namespace) -> int:
    loaded = _load(args)
    inputs = _read_canonical(loaded.path(DISCLOSURE_INPUTS), "disclosure inputs")
    old = inputs.get("predecessor_comparison")
    if not isinstance(old, dict):
        raise ReleaseInputsError(
            "the disclosure inputs carry no predecessor comparison"
        )
    new = _derived_predecessor(args.data_root, old)
    if old == new:
        print(
            json.dumps(
                {
                    "status": "already_current",
                    "manifest_sha256": loaded.manifest_sha256,
                }
            )
        )
        return 0
    out = args.out
    if _nested(out, loaded.root) or _nested(loaded.root, out):
        raise ReleaseInputsError(
            "the new root must be a separate directory: it is, lies inside or contains the source root"
        )
    if out.exists() and any(out.iterdir()):
        raise ReleaseInputsError(f"rebind target is not empty: {out}")
    inside = REPO_ROOT.resolve()
    if out.resolve() == inside or inside in out.resolve().parents:
        raise ReleaseInputsError(
            "the release-inputs root must be outside the repository"
        )
    old_release, old_panel = (
        str(old.get("release_id")),
        str(old.get("panel_generation_id")),
    )
    stale = _stale_references(
        loaded.root,
        {DISCLOSURE_INPUTS, "release-inputs.manifest.json"},
        {
            f"old release {old_release[:8]}": old_release.encode(),
            f"old panel {old_panel[:8]}": old_panel.encode(),
        },
    )
    if stale and not args.allow_stale_references:
        raise ReleaseInputsError(
            "other files still name the old predecessor (rebind them or pass "
            "--allow-stale-references):\n  " + "\n  ".join(stale)
        )
    disclosure = _canonical({**inputs, "predecessor_comparison": new})
    # every source file the new root rewrites is read and checked BEFORE anything is copied
    policy = _read_canonical(loaded.path(POLICY_MANIFEST), "sleeve policy manifest")
    entry = policy.get(POLICY_DISCLOSURE_KEY)
    if (
        not isinstance(entry, dict)
        or "sha256" not in entry
        or "size_bytes" not in entry
    ):
        raise ReleaseInputsError(
            "the sleeve policy manifest does not pin the disclosure inputs"
        )
    policy[POLICY_DISCLOSURE_KEY] = {
        **entry,
        "sha256": hashlib.sha256(disclosure).hexdigest(),
        "size_bytes": len(disclosure),
    }
    # build next to the target and publish by one rename: a refusal leaves no half-written root behind
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{out.name}.building-", dir=out.parent))
    try:
        shutil.copytree(loaded.root, staging, symlinks=False, dirs_exist_ok=True)
        (staging / "release-inputs.manifest.json").unlink()
        rewritten = {DISCLOSURE_INPUTS, POLICY_MANIFEST}
        for relative in loaded.paths:
            if relative in rewritten:
                continue
            copied = staging.joinpath(*relative.split("/"))
            if not copied.is_file() or hashlib.sha256(
                copied.read_bytes()
            ).hexdigest() != loaded.sha256(relative):
                raise ReleaseInputsError(
                    f"{relative} differs from the pinned manifest in the copy "
                    "(the source changed while it was copied): nothing is published"
                )
        (staging / DISCLOSURE_INPUTS).write_bytes(disclosure)
        (staging / POLICY_MANIFEST).write_bytes(_canonical(policy))
        _, pin = build_manifest(staging, list(loaded.paths), cutoff=loaded.cutoff)
        # the rebuilt root must load under the new pin, and its predecessor must be the committed release
        load_release_inputs(staging, pin)
        try:
            sleeve_disclosures.check_predecessor_is_current_release(new, args.data_root)
        except sleeve_disclosures.DisclosureError as exc:
            raise ReleaseInputsError(str(exc)) from exc
        if out.exists():
            out.rmdir()  # empty (checked above)
        os.replace(staging, out)
    except BaseException:
        shutil.rmtree(
            staging, ignore_errors=True
        )  # our own staging directory, never a published root
        raise
    print(
        json.dumps(
            {
                "status": "rebound",
                "release_id": new["release_id"],
                "panel_generation_id": new["panel_generation_id"],
                "manifest_sha256": pin,
                "stale_references": stale,
            }
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("census", "extract"):
        command = commands.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=REPO_ROOT)
        command.add_argument("--commit", default="HEAD")
    extract = commands.choices["extract"]
    extract.add_argument("--out-root", type=Path, required=True)
    extract.add_argument("--cutoff", required=True)
    for name in ("verify", "path"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--manifest-sha256", required=True)
        command.add_argument("--cutoff", help="refuse a set built for another cutoff")
    commands.choices["path"].add_argument("--logical", required=True)
    add = commands.add_parser("add")
    add.add_argument("--root", type=Path, required=True)
    add.add_argument("--manifest-sha256", required=True)
    add.add_argument("--cutoff", help="refuse a set built for another cutoff")
    add.add_argument(
        "--out", type=Path, required=True, help="the new root (empty or absent)"
    )
    add.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="LOGICAL=SOURCE",
        help="a release-input path and the file whose bytes it takes",
    )
    add.add_argument(
        "--replace", action="store_true", help="swap a path the root holds"
    )
    new = commands.add_parser("new-cutoff")
    new.add_argument("--root", type=Path, required=True, help="the source root")
    new.add_argument("--manifest-sha256", required=True, help="the source root's pin")
    new.add_argument(
        "--cutoff", required=True, help="the NEW root's cutoff (YYYY-MM-DD)"
    )
    new.add_argument(
        "--out", type=Path, required=True, help="the new root (must not exist)"
    )
    new.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="ENTRY=PATH",
        help="an entry and the file whose bytes it takes (replaces or adds)",
    )
    new.add_argument("--drop", action="append", default=[], metavar="ENTRY")
    new.add_argument(
        "--carry-stale",
        action="append",
        default=[],
        metavar="ENTRY=REASON",
        help="carry a cutoff-dependent entry unchanged, with the reason (ENTRY may be a registry-style glob)",
    )
    new.add_argument(
        "--baseline",
        type=Path,
        help="shorthand for --file acceptance/expected-baseline.json=PATH (its cutoff header must match)",
    )
    rebind = commands.add_parser("rebind-predecessor")
    rebind.add_argument("--root", type=Path, required=True)
    rebind.add_argument("--manifest-sha256", required=True)
    rebind.add_argument("--cutoff", help="refuse a set built for another cutoff")
    rebind.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="the workspace holding the committed release",
    )
    rebind.add_argument(
        "--out", type=Path, required=True, help="the new root (must be empty or absent)"
    )
    rebind.add_argument("--allow-stale-references", action="store_true")
    args = parser.parse_args(argv)
    handlers = {
        "census": _census,
        "extract": _extract,
        "add": _add,
        "new-cutoff": _new_cutoff,
        "verify": _verify,
        "path": _path,
        "rebind-predecessor": _rebind_predecessor,
    }
    try:
        return handlers[args.command](args)
    except (ReleaseInputsError, OSError) as exc:
        print(f"release-inputs: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
