"""Regression-corpus gate (#911): every defect has a named case, every closed ticket cites one.

``tests/regression/registry.json`` lists one case per defect. A case is ``open`` (no test yet) or
``covered`` (a test in ``tests/regression`` that fails on the pre-fix code; ``kill`` says how).
Tickets map to the cases that prove their fix, or are exempt with a written reason.

Two modes::

    check_regression_corpus.py                      structure: registry is consistent (CI)
    check_regression_corpus.py --release --closed 894 896 ...
                                                    release gate: no open case, and every
                                                    closed ticket cites a covered case

Exit 0 on pass, 1 on findings (every finding printed, not the first), 2 on a usage error.
Value-free: ids, paths and ticket numbers only.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Sequence
import json
from pathlib import Path
import re
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "tests" / "regression" / "registry.json"
CASE_ID = re.compile(r"^[a-z][a-z0-9_]{2,60}$")
TICKET = re.compile(r"^[0-9]{1,6}$")


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # module level only: pytest does not collect a nested def
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def check_structure(registry: dict[str, Any], root: Path = ROOT) -> list[str]:
    found: list[str] = []
    cases = registry.get("cases")
    tickets = registry.get("tickets")
    if not isinstance(cases, dict) or not isinstance(tickets, dict):
        return ["registry needs object keys 'cases' and 'tickets'"]
    referenced: set[str] = set()
    used_tests: dict[str, str] = {}
    for case_id, case in cases.items():
        if not CASE_ID.fullmatch(case_id):
            found.append(f"case id {case_id!r} is not snake_case")
        if not str(case.get("defect", "")).strip():
            found.append(f"{case_id}: defect description missing")
        status = case.get("status")
        if status == "open":
            continue
        if status != "covered":
            found.append(f"{case_id}: status must be open or covered")
            continue
        if not str(case.get("kill", "")).strip():
            found.append(
                f"{case_id}: covered case must say how it fails on pre-fix code"
            )
        node = str(case.get("test", ""))
        path_text, _, func = node.partition("::")
        path = (root / path_text).resolve()
        folder = (root / "tests" / "regression").resolve()
        if folder not in path.parents or not path.is_file():
            found.append(
                f"{case_id}: test file {path_text!r} not under tests/regression"
            )
        elif func not in _test_functions(path):
            found.append(f"{case_id}: {func!r} is not a test function in {path_text}")
        elif node in used_tests:
            found.append(f"{case_id}: shares test {node} with {used_tests[node]}")
        else:
            used_tests[node] = case_id
            referenced.add(path.name)
    for ticket, entry in tickets.items():
        if not TICKET.fullmatch(ticket):
            found.append(f"ticket key {ticket!r} is not a number")
        cited = entry.get("cases", [])
        if not isinstance(cited, list):
            found.append(f"#{ticket}: cases must be a list")
            continue
        exempt = str(entry.get("exempt", "")).strip()
        if bool(cited) == bool(exempt):
            found.append(f"#{ticket}: give either cases or an exempt reason")
        for case_id in cited:
            if case_id not in cases:
                found.append(f"#{ticket}: cites unknown case {case_id!r}")
    folder = root / "tests" / "regression"
    for path in sorted(folder.glob("test_*.py")):
        if path.name != "test_corpus_gate.py" and path.name not in referenced:
            found.append(f"{path.name}: test file is not the test of any covered case")
    return found


def check_release(
    registry: dict[str, Any], closed: Sequence[str], root: Path = ROOT
) -> list[str]:
    found = check_structure(registry, root)
    if not closed:
        found.append("release gate needs the closed ticket list (--closed)")
    cases = registry.get("cases", {})
    for case_id, case in cases.items():
        if case.get("status") != "covered":
            found.append(f"{case_id}: open case (no regression test yet)")
    tickets = registry.get("tickets", {})
    for ticket in closed:
        entry = tickets.get(ticket)
        if entry is None:
            found.append(f"#{ticket}: closed ticket cites no regression case")
            continue
        for case_id in entry.get("cases", []):
            if cases.get(case_id, {}).get("status") != "covered":
                found.append(f"#{ticket}: cited case {case_id} is not covered")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--closed", nargs="*", default=[], metavar="TICKET")
    args = parser.parse_args(argv)
    if args.closed and not args.release:
        parser.error("--closed needs --release")
    if args.release and not args.closed:
        parser.error("--release needs --closed with at least one ticket")
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    found = (
        check_release(registry, args.closed)
        if args.release
        else check_structure(registry)
    )
    for line in found:
        print(f"REGRESSION_CORPUS: {line}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
