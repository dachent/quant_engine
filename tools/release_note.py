"""One-page release note (#908): the generated owner view first, then the record.

    python tools/release_note.py --panel-validation <root>/validation/<generation> \
        [--targets-log validate-targets.log] --out RELEASE-NOTE.md

Reads what the validator already wrote beside ``findings.json`` (``owner-view.json`` and
``owner-view.md``); it classifies nothing itself and imports no acceptance code.  Derived
per-target findings named in the validate-release-targets log are listed by class, so a
target that is not clean can never be missing from the note.  Exit 1 if the owner view is
absent: a release note without it must not be produced.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

_TARGET_LINE = re.compile(r"^validate_release_targets: (?P<name>\S+) -> (?P<path>.+)$")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not an object")
    return value


def target_lines(log: Path) -> list[str]:
    lines: list[str] = []
    for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _TARGET_LINE.match(raw.strip())
        if not match:
            continue
        name, path = match["name"], Path(match["path"].strip())
        try:
            summary = _load(path).get("summary") or {}
        except (OSError, ValueError):
            lines.append(f"- {name}: findings file unreadable (treat as not clean)")
            continue
        counts = ", ".join(f"{k} {v}" for k, v in sorted(summary.items()) if v)
        lines.append(f"- {name}: {counts or 'clean'}")
    return lines


def render(validation_dir: Path, targets_log: Path | None) -> str:
    view_md = validation_dir / "owner-view.md"
    findings = _load(validation_dir / "findings.json")
    _load(validation_dir / "owner-view.json")  # must exist and parse
    parts = [view_md.read_text(encoding="utf-8").rstrip(), "", "## For the record", ""]
    parts += [
        f"- Panel generation: {findings.get('generation_id')}",
        f"- Validator commit: {findings.get('validator_commit')}",
        f"- Expected baseline sha256: {findings.get('expected_baseline_sha256')}",
        f"- Panel findings: {json.dumps(findings.get('summary'), sort_keys=True)}",
    ]
    if targets_log is not None:
        parts += ["", "### Other targets", ""] + (
            target_lines(targets_log) or ["- none listed"]
        )
    return "\n".join(parts) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--panel-validation", type=Path, required=True)
    parser.add_argument("--targets-log", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        text = render(args.panel_validation, args.targets_log)
    except (OSError, ValueError) as exc:
        print(f"release_note: ERROR: {exc}", file=sys.stderr)
        return 1
    temporary = args.out.with_name(args.out.name + ".tmp")
    temporary.write_bytes(text.encode("utf-8"))
    os.replace(temporary, args.out)
    print(f"release_note: wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
