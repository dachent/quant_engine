"""Merge readiness probe for quant_engine pull requests.

Answers one question with one bit: is this PR actually mergeable right now?
Green required checks alone are NOT merge readiness — review conversations and
branch-protection state are enforced at the merge gate, not in CI (see the
PR #363/#365 RCCA in issues #328/#329).

Exit codes:
  0 = READY (mergeStateStatus CLEAN, zero unresolved threads, checks complete)
  1 = NOT READY (prints the named unmet rule)
  2 = usage / API error

Usage: python tools/merge_readiness.py <pr_number> [--require-checks]
"""

from __future__ import annotations

import json
import subprocess
import sys

REPO = "dachent/quant_engine"


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh command failed")
    return result.stdout


def main() -> int:
    args = [item for item in sys.argv[1:] if item != "--require-checks"]
    require_checks = "--require-checks" in sys.argv
    if len(args) != 1 or not args[0].isdigit():
        print(
            "usage: merge_readiness.py <pr_number> [--require-checks]", file=sys.stderr
        )
        return 2
    number = int(args[0])

    pr = json.loads(
        _gh(
            "pr",
            "view",
            str(number),
            "--json",
            "state,headRefOid,mergeStateStatus",
        )
    )

    failures: list[str] = []
    if pr.get("state") != "OPEN":
        failures.append(f"state={pr.get('state')} (not OPEN)")

    threads = json.loads(
        _gh(
            "api",
            "graphql",
            "-f",
            "query=query($owner:String!,$name:String!,$num:Int!){"
            " repository(owner:$owner,name:$name){ pullRequest(number:$num){"
            " reviewThreads(first:100){ nodes{ isResolved } } } } }",
            "-f",
            f"owner={REPO.split('/')[0]}",
            "-f",
            f"name={REPO.split('/')[1]}",
            "-F",
            f"num={number}",
        )
    )["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    unresolved = sum(1 for thread in threads if not thread["isResolved"])
    if unresolved:
        failures.append(f"{unresolved} unresolved review conversation(s)")

    state = pr.get("mergeStateStatus")
    if state not in {"CLEAN", "HAS_HOOKS"}:
        failures.append(f"mergeStateStatus={state}")

    if require_checks:
        checks = json.loads(
            _gh("api", f"repos/{REPO}/commits/{pr.get('headRefOid')}/check-runs")
        )
        pending = [
            item.get("name", "?")
            for item in checks.get("check_runs", [])
            if item.get("status") != "completed"
            or item.get("conclusion") not in {"success", "skipped", "neutral"}
        ]
        if pending:
            failures.append(f"checks not green: {', '.join(pending)}")

    head = pr.get("headRefOid")
    print(
        json.dumps(
            {
                "ready": not failures,
                "pr": number,
                "head": head,
                "merge_state": state,
                "unresolved_threads": unresolved,
                "failures": failures,
            },
            indent=1,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, KeyError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2) from error
