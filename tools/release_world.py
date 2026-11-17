"""Copy a predecessor release world into a new one and repoint its JSON files, as a committed command (refs #418).

A release pass runs in a *world* directory: ``cm`` (cycle manifests and seeds), ``in`` (bundles), ``l0``, ``finality``,
``frozen-inputs`` and the per-provider extracts. A new pass starts from a copy of the previous world with every absolute path
that names the old world repointed. Hand scripts did that with ``sed`` on a name token; the provenance seed stores paths
JSON-escaped (``C:\\\\Dev\\\\...``), the mixed result named a directory that did not exist and the failure surfaced two
stages later (``portfolio_provenance``: ``root design-intake is not a directory``).

``copy`` rewrites the **whole old world root** in each of its spellings (forward slash, JSON-escaped backslash, single
backslash) inside ``*.json`` files only, then **refuses** if any copied JSON still contains the old world's directory name,
so an unknown spelling is a named refusal instead of a late stage failure. It never touches the old world, refuses an
existing destination, and prints the plan unless ``--apply`` is given. ``--roots-file`` writes the roots file the cycle
manifest commands read.

    python tools/release_world.py copy --from C:/Dev/scratch/qe-r3/world-B2 --to C:/Dev/scratch/qe-r3/world-X \\
        [--dirs a,b,c (default: every subdirectory)] [--swap in/sector-d=SRC ...] [--roots-file roots.json --data-root D] [--apply]
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import shutil
import sys


class WorldError(ValueError):
    """The copy cannot be made safely."""


def spellings(root: Path) -> list[str]:
    """The ways an absolute world path appears in JSON text: ``/``, ``\\`` and JSON-escaped ``\\\\``."""

    text = str(root)
    forward = text.replace("\\", "/")
    backslash = forward.replace("/", "\\")
    return [forward, backslash.replace("\\", "\\\\"), backslash]


def rewrite_text(text: str, old: Path, new: Path) -> str:
    """Longest spelling first so the escaped form is never half-replaced by the single-backslash one."""

    pairs = list(zip(spellings(old), spellings(new), strict=True))
    for before, after in sorted(pairs, key=lambda pair: -len(pair[0])):
        text = text.replace(before, after)
    return text


def plan(src: Path, dst: Path, dirs: Sequence[str]) -> list[str]:
    if not src.is_dir():
        raise WorldError(f"source world is not a directory: {src}")
    if dst.exists():
        raise WorldError(f"destination world exists (create-only): {dst}")
    if src.resolve() == dst.resolve() or src.resolve() in dst.resolve().parents:
        raise WorldError("destination must not be the source or inside it")
    missing = [name for name in dirs if not (src / name).is_dir()]
    if missing:
        raise WorldError(f"source world lacks {missing}")
    return [f"copy {src / name} -> {dst / name}" for name in dirs]


def _swap(dst: Path, relative: str, source: Path) -> None:
    """Replace ``dst/relative`` (a bundle or a world-local provider root) with ``source``; keep the old one aside."""

    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise WorldError(
            f"swap target must be a relative path inside the world: {relative}"
        )
    if not source.exists():
        raise WorldError(f"swap source does not exist: {source}")
    target = dst.joinpath(*parts)
    aside = target.with_name(target.name + ".superseded")
    if aside.exists():
        raise WorldError(f"{aside.name} already exists")
    if target.exists():
        target.rename(aside)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copyfile(source, target)


def copy_world(
    src: Path,
    dst: Path,
    dirs: Sequence[str],
    swaps: Sequence[tuple[str, Path]] = (),
) -> dict[str, int]:
    """Copy ``dirs``, repoint the JSON files, refuse on a leftover old-world name, then apply ``swaps``.

    Swaps run after the rewrite: their content comes from elsewhere and its paths are not this world's.
    """

    plan(src, dst, dirs)
    leaf = src.name
    rewritten = scanned = 0
    try:
        dst.mkdir(parents=True)
        for name in dirs:
            shutil.copytree(src / name, dst / name)
        for path in sorted(dst.rglob("*.json")):
            scanned += 1
            try:
                text = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
            changed = rewrite_text(text, src, dst)
            if changed != text:
                path.write_bytes(
                    changed.encode("utf-8")
                )  # bytes: no newline translation
                rewritten += 1
            if leaf in changed:
                raise WorldError(
                    f"{path.relative_to(dst).as_posix()} still names the old world {leaf!r} "
                    "in a spelling this tool does not rewrite"
                )
        for relative, source in swaps:
            _swap(dst, relative, source)
    except Exception:
        # a refused copy leaves no half world behind
        shutil.rmtree(dst, ignore_errors=True)
        raise
    return {
        "dirs": len(dirs),
        "json_scanned": scanned,
        "json_rewritten": rewritten,
        "swaps": len(swaps),
    }


def write_roots(path: Path, data_root: Path, world: Path) -> None:
    if path.exists():
        raise WorldError(f"roots file exists (create-only): {path}")
    path.write_bytes(
        (
            json.dumps(
                {"data": data_root.as_posix(), "world": world.as_posix()},
                indent=1,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    )


def derive_cycle_manifest(
    base: Mapping[str, object],
    *,
    cycle_id: str,
    predecessor: str,
    generations: Mapping[str, tuple[str, str]] | None = None,
    roots: Mapping[str, tuple[str, str]] | None = None,
) -> dict[str, object]:
    """A new cycle manifest from the previous one: new id and predecessor, optional provider overrides.

    ``generations`` maps provider -> (generation id, manifest sha256) and switches that provider to the
    explicit mode; ``roots`` maps provider -> (root key, relative path), for a world-local provider root.
    Everything else is carried. The result is checked with the cycle-manifest validator.
    """

    import copy

    import release_cycle_manifest

    manifest = copy.deepcopy(dict(base))
    providers = manifest["providers"]
    assert isinstance(providers, dict)
    for name in (*(generations or {}), *(roots or {})):
        if name not in providers:
            raise WorldError(f"cycle manifest has no provider {name!r}")
    manifest["cycle_id"] = cycle_id
    manifest["predecessor"] = {"release_id": predecessor}
    for name, (generation_id, manifest_sha) in (generations or {}).items():
        providers[name]["generation"] = {
            "mode": "explicit",
            "generation_id": generation_id,
        }
        providers[name]["manifest_sha256"] = manifest_sha
    for name, (root_key, relative) in (roots or {}).items():
        providers[name]["root"] = {"root_key": root_key, "relative": relative}
    try:
        release_cycle_manifest.validate_static(manifest)
    except release_cycle_manifest.CycleManifestError as exc:
        raise WorldError(f"derived cycle manifest is invalid: {exc}") from exc
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    copy = commands.add_parser("copy")
    copy.add_argument("--from", dest="src", type=Path, required=True)
    copy.add_argument("--to", dest="dst", type=Path, required=True)
    copy.add_argument("--dirs", default="")
    copy.add_argument("--roots-file", type=Path)
    copy.add_argument("--data-root", type=Path)
    copy.add_argument(
        "--swap",
        action="append",
        default=[],
        metavar="RELATIVE=SOURCE",
        help="replace a bundle or provider root inside the new world (old one kept as .superseded)",
    )
    copy.add_argument("--apply", action="store_true")
    cm = commands.add_parser("cycle-manifest")
    cm.add_argument("--from", dest="src", type=Path, required=True)
    cm.add_argument("--out", type=Path, required=True)
    cm.add_argument("--cycle-id", required=True)
    cm.add_argument("--predecessor", required=True)
    cm.add_argument(
        "--generation",
        action="append",
        default=[],
        metavar="PROVIDER=ID:MANIFEST_SHA256",
    )
    cm.add_argument(
        "--root", action="append", default=[], metavar="PROVIDER=ROOT_KEY:RELATIVE"
    )
    return parser


def _pairs(items: Sequence[str], what: str) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for item in items:
        name, _, rest = item.partition("=")
        left, _, right = rest.partition(":")
        if not name or not left or not right:
            raise WorldError(f"{what} needs NAME=A:B, got {item!r}")
        result[name] = (left, right)
    return result


def _cycle_manifest(args: argparse.Namespace) -> int:
    base = json.loads(args.src.read_bytes())
    manifest = derive_cycle_manifest(
        base,
        cycle_id=args.cycle_id,
        predecessor=args.predecessor,
        generations=_pairs(args.generation, "--generation"),
        roots=_pairs(args.root, "--root"),
    )
    raw = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")
    if args.out.exists():
        raise WorldError(f"output exists (create-only): {args.out}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(raw)
    print(
        json.dumps(
            {
                "cycle_manifest": args.out.as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "cycle-manifest":
        try:
            return _cycle_manifest(args)
        except (WorldError, OSError, ValueError) as exc:
            print(f"release-world: FAIL: {exc}", file=sys.stderr)
            return 1
    dirs = [name for name in args.dirs.split(",") if name]
    if not dirs and args.src.is_dir():
        dirs = sorted(p.name for p in args.src.iterdir() if p.is_dir())
    try:
        steps = plan(args.src, args.dst, dirs)
        if args.roots_file is not None and args.data_root is None:
            raise WorldError("--roots-file needs --data-root")
        if not args.apply:
            print("\n".join(steps) + "\n(dry run: pass --apply)")
            return 0
        swaps = []
        for item in args.swap:
            relative, _, source = item.partition("=")
            if not relative or not source:
                raise WorldError(f"--swap needs RELATIVE=SOURCE, got {item!r}")
            swaps.append((relative, Path(source)))
        result = copy_world(args.src, args.dst, dirs, swaps)
        if args.roots_file is not None:
            write_roots(args.roots_file, args.data_root, args.dst)
    except (WorldError, OSError) as exc:
        print(f"release-world: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
