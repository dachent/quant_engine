"""Refuse release publication unless the generation's findings show zero blockers.

This closes the same failure class named by three issues: #469 (conformance
blocking reasons are computed but nothing reads them before publish), #326
(an applicable open correction must block release, not just get recorded),
and #444 (a computed signal that should stop delivery is silently skipped).
In every case a detector already runs and already knows the answer; nothing
downstream refuses on it. The fix is one generalized precondition rather than
a bespoke gate per symptom: read the one findings artifact ``validate_generation``
(built separately) emits for a generation, and refuse to publish it if that
artifact is absent or names a blocker.

Contract (owned by the validator, consumed here strictly by path/shape --
this tool never calls the validator and never re-derives its verdict):

    findings.json = a JSON list of objects, each with:
        check_id:    str
        class:       one of BLOCKER_INVARIANT | BLOCKER_AUTHORITY |
                     BLOCKER_REGRESSION | NOT_READY | WARNING
        entity:      str
        observed:    Any
        expected:    Any
        pointer:     str
        defect_ids:  list[str]

    Zero ``BLOCKER_*`` entries is the pass condition. ``NOT_READY`` and
    ``WARNING`` are recorded but do not, by themselves, refuse.

A missing findings file is refused, not treated as "no findings": absence of
proof is not proof of absence, and a validator that has not run yet must not
be read as a validator that found nothing wrong.

Candidate (unpublished) generations are unaffected -- this only guards the
specific generation about to be published, named explicitly by the caller,
never a directory sweep of everything retained.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REQUIRED_FINDING_KEYS = (
    "check_id",
    "class",
    "entity",
    "observed",
    "expected",
    "pointer",
    "defect_ids",
)

ALLOWED_CLASSES = frozenset(
    {
        "BLOCKER_INVARIANT",
        "BLOCKER_AUTHORITY",
        "BLOCKER_REGRESSION",
        "NOT_READY",
        "WARNING",
    }
)

BLOCKER_PREFIX = "BLOCKER_"


class FindingsContractError(ValueError):
    """The findings file does not match the contract ``validate_generation`` defines."""


def findings_path(findings_root: Path, generation_id: str) -> Path:
    """The canonical location of a generation's findings file.

    ``validate_generation`` is built and owned by another lane; this is the
    path convention this gate enforces against, not a guess at its internals.
    """
    return findings_root / generation_id / "findings.json"


def _validate_finding(entry: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise FindingsContractError(f"findings[{index}] is not an object")
    missing_keys = [key for key in REQUIRED_FINDING_KEYS if key not in entry]
    if missing_keys:
        raise FindingsContractError(
            f"findings[{index}] is missing required keys: {sorted(missing_keys)}"
        )
    finding_class = entry["class"]
    if finding_class not in ALLOWED_CLASSES:
        raise FindingsContractError(
            f"findings[{index}] has an unrecognized class: {finding_class!r}"
        )
    if not isinstance(entry["defect_ids"], list):
        raise FindingsContractError(f"findings[{index}].defect_ids is not a list")
    return entry


def load_findings(path: Path) -> list[dict[str, Any]]:
    """Parse and validate a findings file. Raises on any contract violation.

    Fails closed: a findings file that cannot be read or does not match the
    declared shape is treated the same as one naming a blocker, never as a
    clean pass. A validator that emits nonsense is not evidence of readiness.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FindingsContractError(f"findings file cannot be read: {exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise FindingsContractError(f"findings file is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise FindingsContractError("findings file is not a JSON list")
    return [
        _validate_finding(entry, index=index) for index, entry in enumerate(payload)
    ]


def blocking_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry for entry in findings if str(entry["class"]).startswith(BLOCKER_PREFIX)
    ]


def evaluate(path: Path) -> tuple[bool, list[dict[str, Any]], str | None]:
    """Return ``(may_publish, blocking_findings, refusal_reason)``.

    ``refusal_reason`` is set only for a missing/malformed file; a well-formed
    file with blocking entries returns those entries instead, so the caller
    can report exactly what refused it.
    """
    if not path.is_file():
        return False, [], f"no findings file for this generation at {path}"
    try:
        findings = load_findings(path)
    except FindingsContractError as exc:
        return False, [], str(exc)
    return not blocking_findings(findings), blocking_findings(findings), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generation-id",
        required=True,
        help="the exact generation about to be published",
    )
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument(
        "--findings",
        type=Path,
        help="explicit path to that generation's validate_generation findings.json",
    )
    location.add_argument(
        "--findings-root",
        type=Path,
        help="directory holding <findings-root>/<generation-id>/findings.json",
    )
    arguments = parser.parse_args(argv)

    path = (
        arguments.findings
        if arguments.findings is not None
        else findings_path(arguments.findings_root, arguments.generation_id)
    )

    may_publish, blockers, refusal_reason = evaluate(path)

    if refusal_reason is not None:
        print(
            f"REFUSED: generation {arguments.generation_id} -- {refusal_reason}",
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
