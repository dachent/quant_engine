"""Merge readiness probe for quant_engine pull requests.

Answers one question with one bit: is this PR actually mergeable right now?
Green required checks alone are NOT merge readiness — review conversations and
branch-protection state are enforced at the merge gate, not in CI (see the
PR #363/#365 RCCA in issues #328/#329).

Self-referential safety: when this script runs AS the "readiness" check it
excludes its own kind from the failure/pending census. Otherwise two instances
would each observe the other's failing state, keep the branch UNSTABLE, and
deadlock forever.

Exit codes:
  0 = READY (open, zero unresolved threads, no non-self failures/pending)
  1 = NOT READY (prints the named unmet rule)
  2 = usage / API error

Usage: python tools/merge_readiness.py <pr_number>
"""

from __future__ import annotations

import json
import subprocess
import sys

REPO = "dachent/quant_engine"
SELF_CHECK_NAME = "readiness"


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh command failed")
    return result.stdout


def _unresolved_thread_count(number: int) -> int:
    """Count unresolved review threads across ALL pages."""
    owner, name = REPO.split("/")
    cursor: str | None = None
    unresolved = 0
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        payload = json.loads(
            _gh(
                "api",
                "graphql",
                "-f",
                "query=query($owner:String!,$name:String!,$num:Int!){"
                " repository(owner:$owner,name:$name){ pullRequest(number:$num){"
                " reviewThreads(first:100"
                + after
                + "){ pageInfo{ hasNextPage endCursor }"
                " nodes{ isResolved } } } } }",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"num={number}",
            )
        )
        threads = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
        unresolved += sum(1 for node in threads["nodes"] if not node["isResolved"])
        if not threads["pageInfo"]["hasNextPage"]:
            return unresolved
        cursor = threads["pageInfo"]["endCursor"]


def _other_check_states(head_sha: str) -> tuple[list[str], list[str]]:
    """Return (failing_names, pending_names) for every check except SELF_CHECK_NAME."""
    payload = json.loads(
        _gh("api", f"repos/{REPO}/commits/{head_sha}/check-runs?per_page=100")
    )
    failing: list[str] = []
    pending: list[str] = []
    for item in payload.get("check_runs", []):
        name = item.get("name", "?")
        if name == SELF_CHECK_NAME:
            continue
        if item.get("status") != "completed":
            pending.append(name)
        elif item.get("conclusion") not in {"success", "skipped", "neutral"}:
            failing.append(name)
    return failing, pending


def main() -> int:
    args = sys.argv[1:]
    if len(args) != 1 or not args[0].isdigit():
        print("usage: merge_readiness.py <pr_number>", file=sys.stderr)
        return 2
    number = int(args[0])

    pr = json.loads(
        _gh("pr", "view", str(number), "--json", "state,headRefOid,mergeStateStatus")
    )

    failures: list[str] = []
    if pr.get("state") != "OPEN":
        failures.append(f"state={pr.get('state')} (not OPEN)")

    head = pr.get("headRefOid")
    unresolved = _unresolved_thread_count(number)
    if unresolved:
        failures.append(f"{unresolved} unresolved review conversation(s)")

    failing, pending = _other_check_states(head)
    if failing:
        failures.append(f"failing checks: {', '.join(sorted(failing))}")
    if pending:
        failures.append(f"pending checks: {', '.join(sorted(pending))}")

    # CLEAN/HAS_HOOKS are unconditionally acceptable. UNSTABLE is acceptable
    # only when nothing outside this check explains it (i.e., the instability
    # is our own prior runs); anything else was already reported above.
    state = pr.get("mergeStateStatus")
    stable = state in {"CLEAN", "HAS_HOOKS"}
    self_caused_unstable = (
        state == "UNSTABLE" and not failing and not pending and not unresolved
    )
    if not stable and not self_caused_unstable:
        failures.append(f"mergeStateStatus={state}")

    print(
        json.dumps(
            {
                "ready": not failures,
                "pr": number,
                "head": head,
                "merge_state": state,
                "unresolved_threads": unresolved,
                "other_failing_checks": sorted(failing),
                "other_pending_checks": sorted(pending),
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
