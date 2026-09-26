"""Standalone CLI to check a generation's validate_generation findings.

This is a thin, human-facing wrapper: the actual gate is enforced at the
publication sink itself (`commit_release`,
packages/core/src/quant_engine/core/release.py), not here, so no caller can
publish without a clean findings file just because it forgot to run this
tool. This CLI exists for an operator or a script to get a fast, standalone
answer -- "would this generation be allowed to publish right now?" -- against
the same contract and the same code
(`quant_engine.core.publication.evaluate_release_validation_findings`) the
sink itself uses.

Contract (owned by the validator lane, consumed here strictly by path/shape;
see `state/briefs/findings-contract.md` and
`packages/core/src/quant_engine/core/publication.py`): a findings document is
a JSON object with `schema`, `generation_id`, `generation_dir`,
`validator_commit`, `expected_baseline_sha256`, `summary`, and `findings` (a
list of `{check_id, class, entity, observed, expected, pointer, defect_ids}`,
`class` one of `BLOCKER_INVARIANT|BLOCKER_AUTHORITY|BLOCKER_REGRESSION|
NOT_READY|WARNING`). Zero `BLOCKER_*` findings, a matching `generation_id`,
and a `summary` that agrees with `findings` is the pass condition. A missing,
unreadable, non-resident (cloud-placeholder), malformed, or generation-id-
mismatched file all refuse -- never treated as a clean pass. #326's
"applicable open correction blocks release" maps to a `BLOCKER_AUTHORITY`
finding with a `correction.` `check_id` prefix.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from quant_engine.core.publication import (
    default_release_validation_findings_path,
    evaluate_release_validation_findings,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generation-id",
        required=True,
        help="the exact generation to check",
    )
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument(
        "--findings",
        type=Path,
        help="explicit path to that generation's validate_generation findings.json",
    )
    location.add_argument(
        "--root",
        type=Path,
        help=(
            "the generation's root (e.g. a stage/domain root); the default "
            "location <root>/validation/<generation-id>/findings.json is used"
        ),
    )
    arguments = parser.parse_args(argv)

    path = (
        arguments.findings
        if arguments.findings is not None
        else default_release_validation_findings_path(
            arguments.root, arguments.generation_id
        )
    )

    may_publish, blockers, reason = evaluate_release_validation_findings(
        path, expected_generation_id=arguments.generation_id
    )

    if reason is not None:
        print(
            f"REFUSED: generation {arguments.generation_id} -- {reason}",
            file=sys.stderr,
        )
        return 1

    if not may_publish:
        print(
            f"REFUSED: generation {arguments.generation_id} has "
            f"{len(blockers)} blocking finding(s):",
            file=sys.stderr,
        )
        for entry in blockers:
            print(
                f"  {entry['class']:<20} {entry['check_id']:<32} "
                f"{entry['entity']} -- observed={entry['observed']!r} "
                f"expected={entry['expected']!r} pointer={entry['pointer']} "
                f"defect_ids={entry['defect_ids']}",
                file=sys.stderr,
            )
        return 1

    print(f"CLEARED: generation {arguments.generation_id} -- {path} has zero blockers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
