"""Write the failing set and job timing of a pytest run to the job summary.

Reads the junit file the Quality job writes, lists every failing or erroring test (no
masking: the suite runs without ``--maxfail``), the slowest tests and the pytest wall time.
Prints markdown to stdout and appends it to ``$GITHUB_STEP_SUMMARY`` when set. Exit 0 always:
it reports, the pytest step decides pass/fail. Value-free: node ids and durations only.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET  # noqa: S405 - junit written by our own pytest run


def render(
    junits: Sequence[Path], job_start: float | None = None, slowest: int = 10
) -> str:
    lines = ["## Test run summary", ""]
    present = [j for j in junits if j.is_file()]
    if not present:
        lines.append(
            "No junit file: the test step did not start or was cancelled (a "
            "cancel-in-progress cancel or a timeout), so there is no verdict."
        )
        return "\n".join(lines) + "\n"
    for missing in (j for j in junits if not j.is_file()):
        lines.append(f"- no junit for `{missing.name}`: that step did not report")
    failing: list[str] = []
    cases: list[ET.Element] = []
    wall = 0.0
    partial = False
    for junit in present:
        root = ET.parse(junit).getroot()
        for case in root.iter("testcase"):
            if not case.get("classname"):
                # An interrupted run leaves a nameless case: not a finished test.
                partial = True
                continue
            cases.append(case)
            if case.find("failure") is not None or case.find("error") is not None:
                failing.append(f"{case.get('classname')}::{case.get('name', '')}")
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        wall += sum(float(s.get("time", 0) or 0) for s in suites)
    if partial:
        lines.append(
            "- PARTIAL: the run was interrupted; the failing set below is incomplete."
        )
    lines.append(f"- tests: {len(cases)}, failing: {len(failing)}")
    lines.append(f"- pytest wall time: {wall / 60:.1f} min")
    if job_start is not None:
        lines.append(f"- job elapsed so far: {(time.time() - job_start) / 60:.1f} min")
    if failing:
        lines += ["", f"### Failing tests ({len(failing)})", ""]
        lines += [f"- `{name}`" for name in sorted(failing)]
    timed = sorted(cases, key=lambda c: -float(c.get("time", 0) or 0))[:slowest]
    lines += ["", f"### Slowest {len(timed)} tests", ""]
    lines += [
        f"- {float(c.get('time', 0) or 0):.1f}s `{c.get('classname')}::{c.get('name', '')}`"
        for c in timed
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--junit", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    start = os.environ.get("QE_JOB_START")
    text = render(args.junit, float(start) if start else None)
    print(text)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
