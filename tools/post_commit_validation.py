"""Post-commit validator re-run check (Ruling F section 6, ticket #896).

The validator runs before the commit, when the release pointer still names the
predecessor, so two checks that read the committed release
(``identity.publication_state``, ``identity.release_pinned``) are silent by construction
at the only time they run, and a finding that appears only after the commit is invisible.
The release ceremony therefore runs ``validate_generation.py`` again after the commit,
with an explicit ``--release-pointer`` and its own ``--out``, and this tool compares the
two findings documents:

* the post-commit run has zero ``BLOCKER_*`` rows;
* the set of ``(check_id, entity, class)`` rows equals the pre-commit set, so a row that
  appears or disappears across the commit is a refusal;
* except that the two checks that read the committed release (``identity.release_pinned``,
  ``identity.publication_state``) may carry a NOT_READY "no release record" row only
  before the commit: the post-commit run must have none, which proves the committed
  release was actually read (a first release in a root has no pointer to discover
  before its commit).

The receipt records the sha256 of both findings files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

RECEIPT_SCHEMA = "qe-post-commit-validation/1"
Row = tuple[str, str, str]
COMMIT_DEPENDENT = frozenset({"identity.release_pinned", "identity.publication_state"})


class PostCommitValidationError(ValueError):
    """The findings documents are unreadable or disagree."""


def _load(path: Path) -> tuple[dict[str, Any], str]:
    data = path.read_bytes()
    document = json.loads(data)
    if not isinstance(document, dict) or not isinstance(document.get("findings"), list):
        raise PostCommitValidationError(f"not a findings document: {path.name}")
    return document, hashlib.sha256(data).hexdigest()


def rows(document: dict[str, Any]) -> set[Row]:
    found: set[Row] = set()
    for finding in document["findings"]:
        try:
            found.add(
                (
                    str(finding["check_id"]),
                    str(finding["entity"]),
                    str(finding["class"]),
                )
            )
        except (KeyError, TypeError) as exc:
            raise PostCommitValidationError(
                "a finding lacks id, entity or class"
            ) from exc
    return found


def compare(pre: dict[str, Any], post: dict[str, Any]) -> list[str]:
    """Reasons the post-commit run is not equivalent to the pre-commit run."""

    problems: list[str] = []
    if pre.get("generation_id") != post.get("generation_id"):
        problems.append("the two runs validated different generations")
    before, after = rows(pre), rows(post)
    before = {
        r for r in before if not (r[0] in COMMIT_DEPENDENT and r[2] == "NOT_READY")
    }
    for row in sorted(after):
        if row[2].startswith("BLOCKER_"):
            problems.append(f"post-commit blocker: {row[0]} {row[1]} {row[2]}")
        if row[0] in COMMIT_DEPENDENT and row[2] == "NOT_READY":
            problems.append(
                f"the committed release was not read: {row[0]} {row[1]} is NOT_READY"
            )
    for row in sorted(after - before):
        problems.append(f"row appeared after the commit: {' | '.join(row)}")
    for row in sorted(before - after):
        problems.append(f"row vanished after the commit: {' | '.join(row)}")
    return problems


def check(pre_path: Path, post_path: Path) -> dict[str, Any]:
    pre, pre_sha = _load(pre_path)
    post, post_sha = _load(post_path)
    problems = compare(pre, post)
    return {
        "schema": RECEIPT_SCHEMA,
        "pre_commit_findings_sha256": pre_sha,
        "post_commit_findings_sha256": post_sha,
        "generation_id": post.get("generation_id"),
        "row_count": len(rows(post)),
        "problems": problems,
        "status": "PASS" if not problems else "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pre", type=Path, required=True)
    parser.add_argument("--post", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, help="create-only receipt path")
    args = parser.parse_args(argv)
    try:
        receipt = check(args.pre, args.post)
    except (OSError, ValueError) as exc:
        print(f"post-commit-validation: FAIL: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(receipt, indent=1, sort_keys=True) + "\n"
    if args.receipt is not None:
        try:
            with args.receipt.open("xb") as handle:
                handle.write(text.encode())
        except FileExistsError:
            print(
                "post-commit-validation: FAIL: receipt exists (create-only)",
                file=sys.stderr,
            )
            return 1
    print(text, end="")
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
