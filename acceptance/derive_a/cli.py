"""Command line: ``python acceptance/derive_a --qe-root <dir> --out <file>``."""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .derive import derive, dumps
from .sources import InputError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CUTOFF = "2026-09-17"
DEFAULT_MODEL = "claude-opus-5-5"


def _head_commit(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--", "acceptance"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"
    commit = completed.stdout.strip()
    return commit + ("+dirty" if status.stdout.strip() else "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qe-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--code-commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cutoff = dt.date.fromisoformat(args.cutoff)
    except ValueError:
        print(f"derive_a: invalid --cutoff {args.cutoff!r}", file=sys.stderr)
        return 2
    commit = args.code_commit or _head_commit(REPO_ROOT)
    try:
        document = derive(
            args.qe_root, REPO_ROOT, cutoff=cutoff, model=args.model, code_commit=commit
        )
    except InputError as exc:
        print(f"derive_a: input error: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(dumps(document))
    print(
        f"derive_a: {len(document['metrics'])} metrics, "
        f"{len(document['not_derivable'])} not_derivable -> {args.out}"
    )
    return 0
