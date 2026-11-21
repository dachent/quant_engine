"""Reject NEW lines that look like owner data in a public repository (F2, after the 22a ruling).

The repository is public (owner ruling 2026-11-20).  This check reads only the lines a change ADDS
(``git diff --unified=0 BASE..HEAD``) and refuses two shapes, storing no value:

* a consumer-mail address (a local part at gmail, yahoo, outlook and the usual free-mail domains);
* an account-shaped string ``dddd-dddd`` (also ``dddd dddd`` or a longer tail), outside its exemptions:
  a year range, the synthetic ``0000-`` form, and a number inside a path, line range or version.

``ops/preserved-scripts`` (byte-for-byte recovered evidence) and lock files are not read.  A reviewed synthetic
line carries the marker ``owner-shape: synthetic`` anywhere on it.  It is a tripwire for the common accident, not
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


def findings(diff: str) -> list[str]:
    """``path:line rule`` for each added line that has a forbidden shape (never the value)."""

    out: list[str] = []
    for path, number, text in added_lines(diff):
        if path.startswith(SKIPPED_PREFIXES) or path.endswith(SKIPPED_SUFFIXES):
            continue
        if MARKER in text:
            continue
        for rule, pattern in RULES.items():
            if pattern.search(text):
                out.append(f"{path}:{number} {rule}")
    return out


def _diff(repo_root: Path, base: str, head: str) -> str:
    left = GIT_EMPTY_TREE if base == EMPTY_TREE else base
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
    args = parser.parse_args(argv)
    try:
        hits = findings(_diff(args.repo_root, args.changed_from, args.changed_to))
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
    print("owner-shapes: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
