"""Reject NEW lines that look like owner data in a public repository (F2, after the 22a ruling).

The repository is public (owner ruling 2026-11-20).  This check reads only the lines a change ADDS
(``git diff --unified=0 BASE..HEAD``) and refuses two shapes, storing no value:

* a consumer-mail address (a local part at gmail, yahoo, outlook and the usual free-mail domains);
* an account-shaped string ``dddd-dddd`` (also ``dddd dddd`` or a longer tail), outside its exemptions:
  a year range, the synthetic ``0000-`` form, and a number inside a path, line range or version.

``ops/preserved-scripts`` (byte-for-byte recovered evidence) and lock files are not read.  A reviewed synthetic
line carries the marker ``owner-shape: synthetic``: it is not refused but it is still listed by path:line
in the output, so the marker cannot hide a real value from a reviewer.  It is a tripwire for the common accident, not
a privacy proof: it does not read money, tickers, split values or unchanged lines.

    python tools/check_new_commit_owner_shapes.py --repo-root . --changed-from BASE --changed-to HEAD
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
from typing import Final

EMPTY_TREE: Final = "EMPTY_TREE"
GIT_EMPTY_TREE: Final = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MARKER: Final = "owner-shape: synthetic"
SKIPPED_PREFIXES: Final = ("ops/preserved-scripts/",)
SKIPPED_SUFFIXES: Final = (".lock",)
CONSUMER_MAIL: Final = re.compile(
    r"[\w.+-]+@(?:gmail|googlemail|yahoo|ymail|outlook|hotmail|live|icloud|aol|proton|protonmail)\.(?:com|me)\b",
    re.IGNORECASE,
)
ACCOUNT_SHAPE: Final = re.compile(
    r"(?<![\w.:/~#,+-])(?!(?:19|20)\d\d[- ](?:19|20)\d\d|0000)\d{4}[- ]\d{4,}(?![\w.-])"
)
RULES: Final = {
    "consumer-mail address": CONSUMER_MAIL,
    "account-shaped string": ACCOUNT_SHAPE,
}


class OwnerShapeError(ValueError):
    """The diff could not be read."""


def added_lines(
    diff: str,
) -> list[tuple[str, int, str]]:
    """(path, line number, text) of every added line of a ``--unified=0`` diff."""

    found: list[tuple[str, int, str]] = []
    path = ""
    number = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].removeprefix("b/") if raw[4:] != "/dev/null" else ""
        elif raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            number = int(match.group(1)) if match else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            if path:
                found.append((path, number, raw[1:]))
            number += 1
    return found


def scan(diff: str) -> tuple[list[str], list[str]]:
    """(refused, acknowledged): ``path:line rule`` for each added line with a forbidden shape (never the value).

    A line carrying the marker is acknowledged, not refused, and is STILL NAMED so a reviewer sees every
    marked line: the marker cannot hide a real value, it only moves the line from a failure to a review list.
    """

    refused: list[str] = []
    acknowledged: list[str] = []
    for path, number, text in added_lines(diff):
        if path.startswith(SKIPPED_PREFIXES) or path.endswith(SKIPPED_SUFFIXES):
            continue
        for rule, pattern in RULES.items():
            if pattern.search(text):
                (acknowledged if MARKER in text else refused).append(
                    f"{path}:{number} {rule}"
                )
    return refused, acknowledged


def findings(diff: str) -> list[str]:
    """The refused lines only (see :func:`scan`)."""

    return scan(diff)[0]


def resolve_base(
    repo_root: Path, base: str, head: str, whole_tree: bool = False
) -> str | None:
    """The commit to diff from, or None when there is no meaningful range.

    ``EMPTY_TREE`` is what the workflow passes on the first push of a new branch (``before`` is all zeros).
    Reading the whole tree as "added" would flag every existing file, so it falls back to the merge-base
    with the default branch; with no default branch available the check is skipped (the pull-request run
    still reads the real range).  ``whole_tree`` asks for the whole-tree scan on purpose.
    """

    if base != EMPTY_TREE or whole_tree:
        return GIT_EMPTY_TREE if base == EMPTY_TREE else base
    for ref in ("origin/main", "refs/remotes/origin/main", "main"):
        found = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", ref, head],
            capture_output=True,
            text=True,
            check=False,
        )
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    return None


def _diff(repo_root: Path, left: str, head: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "diff",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--diff-filter=AM",
            left,
            head,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise OwnerShapeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--changed-from", required=True)
    parser.add_argument("--changed-to", default="HEAD")
    parser.add_argument(
        "--whole-tree",
        action="store_true",
        help="with EMPTY_TREE, read the whole tree as added instead of the merge-base range",
    )
    args = parser.parse_args(argv)
    try:
        left = resolve_base(
            args.repo_root, args.changed_from, args.changed_to, args.whole_tree
        )
        if left is None:
            print(
                "owner-shapes: SKIPPED: no base range (new branch, no default branch to compare with); "
                "the pull-request run checks the real range"
            )
            return 0
        hits, acknowledged = scan(_diff(args.repo_root, left, args.changed_to))
    except OwnerShapeError as exc:
        print(f"owner-shapes: FAIL: {exc}", file=sys.stderr)
        return 2
    if hits:
        print(
            "owner-shapes: FAIL: added lines look like owner data (shape only; mark a reviewed "
            f"synthetic line with '{MARKER}'):\n  " + "\n  ".join(hits[:50]),
            file=sys.stderr,
        )
        return 1
    if acknowledged:
        listing = "\n  ".join(acknowledged[:50])
        print(
            f"owner-shapes: {len(acknowledged)} marked line(s) need a reviewer: "
            f"{MARKER}\n  {listing}"
        )
    print("owner-shapes: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
