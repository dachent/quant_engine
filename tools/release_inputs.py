"""Move cutoff-dependent tracked inputs out of Git and verify them (``core.release_inputs``).

``census``   list tracked files by registry disposition (release-input / code-bound / policy).
``extract``  copy every tracked release-input blob of a commit into a NEW root outside the
             repository and write its manifest; prints the manifest sha256 (the pin).
``verify``   load a root against a pin, hashing every file; exit 0 only if all hold.
``path``     print the verified absolute path of one repo-relative input (for shell drivers).

The root and the pin are arguments; nothing is hardcoded. A missing, changed or unpinned
input is a refusal (exit 1), never a fallback to a repo copy.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from quant_engine.core import (
    RELEASE_INPUTS_CODE_BOUND as CODE_BOUND,
    RELEASE_INPUTS_KIND as RELEASE_INPUT,
    RELEASE_INPUTS_POLICY as POLICY,
    ReleaseInputs,
    ReleaseInputsError,
    build_release_inputs_manifest as build_manifest,
    classify_release_input as classify,
    load_release_inputs,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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
    args = parser.parse_args(argv)
    handlers = {
        "census": _census,
        "extract": _extract,
        "verify": _verify,
        "path": _path,
    }
    try:
        return handlers[args.command](args)
    except (ReleaseInputsError, OSError) as exc:
        print(f"release-inputs: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
