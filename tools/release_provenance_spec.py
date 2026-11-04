"""Derive the provenance spec a release cycle stages from, binding the repository's own contract bytes.

RELEASE-DRIVER-001. The provenance spec carries ``financial_mvp``: the financial MVP contract, its
registry and schema, each as an absolute path and a sha256. A carried spec pins the bytes of the cycle
that wrote it, so it goes stale as soon as the contract changes on the frozen head (R3 rehearsal:
``contract_sha256`` 5db45d3f against e0dd4a4b). ``refresh`` writes a copy whose three paths point at
the files of ``--repo-root`` and whose three hashes are those files' bytes. Every other key is carried
unchanged: the date, the roots and the acquisition facts are the operator's, not the repository's.

``refresh``  write ``--out`` create-only; ``--check`` re-derives and refuses when ``--out`` differs.

Exit 0 ok, 1 refusal (``CODE: detail`` on stderr), 2 usage.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SECTION = "financial_mvp"
NAMES = ("contract", "registry", "schema")


class ProvenanceSpecError(Exception):
    """A refusal. ``code`` is the message class; ``str(error)`` is ``CODE: detail``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _in_repo(seed_path: str, repo_root: Path, name: str) -> Path:
    """The file of ``repo_root`` the seed's absolute path names: its longest existing suffix."""

    parts = Path(seed_path).parts
    root = repo_root.resolve()
    for start in range(len(parts)):
        candidate = root.joinpath(*parts[start:])
        if candidate.is_file():
            resolved = candidate.resolve()
            if root not in resolved.parents:
                raise ProvenanceSpecError(
                    "OUTSIDE_REPO", f"{name} resolves outside the repository"
                )
            return resolved
    raise ProvenanceSpecError(
        "MISSING", f"{name} {seed_path!r} has no file of that name in the repository"
    )


def refresh_spec(seed: Mapping[str, Any], repo_root: Path) -> dict[str, Any]:
    if SECTION not in seed:
        # the section is optional in a provenance spec: nothing to bind
        return dict(seed)
    section = seed[SECTION]
    if not isinstance(section, Mapping):
        raise ProvenanceSpecError("SEED", f"{SECTION} is not an object")
    updated = dict(section)
    for name in NAMES:
        key = f"{name}_path"
        value = section.get(key)
        if not isinstance(value, str) or not value:
            raise ProvenanceSpecError("SEED", f"{SECTION}.{key} is required")
        path = _in_repo(value, repo_root, name)
        updated[key] = str(path)
        updated[f"{name}_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = dict(seed)
    spec[SECTION] = updated
    return spec


def render(spec: Mapping[str, Any]) -> bytes:
    return (json.dumps(spec, indent=1, sort_keys=True) + "\n").encode("utf-8")


def refresh(args: argparse.Namespace) -> int:
    try:
        seed = json.loads(args.seed_spec.read_bytes())
    except (OSError, ValueError) as exc:
        raise ProvenanceSpecError("SEED", f"cannot read {args.seed_spec}") from exc
    if not isinstance(seed, dict):
        raise ProvenanceSpecError("SEED", "the seed spec is not an object")
    data = render(refresh_spec(seed, args.repo_root))
    if args.check:
        if not args.out.is_file() or args.out.read_bytes() != data:
            raise ProvenanceSpecError(
                "DIFFERS", f"{args.out} is not what the repository derives"
            )
    elif args.out.exists():
        # a rerun that derives the same bytes completes; anything else is a refusal
        if args.out.read_bytes() != data:
            raise ProvenanceSpecError("OUTPUT_EXISTS", str(args.out))
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as handle:
            handle.write(data)
    print(json.dumps({"spec_sha256": hashlib.sha256(data).hexdigest()}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("refresh", help="bind the repository's contract bytes")
    cmd.add_argument("--seed-spec", type=Path, required=True)
    cmd.add_argument("--out", type=Path, required=True)
    cmd.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    cmd.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return refresh(args)
    except ProvenanceSpecError as exc:
        print(f"release-provenance-spec: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
