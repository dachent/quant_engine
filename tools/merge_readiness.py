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
from datetime import datetime, timezone

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


def _required_check_contexts() -> set[tuple[str, int | None]]:
    payload = json.loads(
        _gh("api", f"repos/{REPO}/branches/main/protection/required_status_checks")
    )
    contexts = payload.get("contexts")
    if not isinstance(contexts, list) or not all(
        isinstance(item, str) and item for item in contexts
    ):
        raise RuntimeError("required status-check contexts are unavailable")
    checks = payload.get("checks", [])
    if not isinstance(checks, list):
        raise RuntimeError("required status-check app bindings are unavailable")
    app_bound: set[tuple[str, int]] = set()
    for raw in checks:
        if not isinstance(raw, dict):
            raise RuntimeError("required status-check app binding is invalid")
        context = raw.get("context")
        app_id = raw.get("app_id")
        if not isinstance(context, str) or not context or not isinstance(app_id, int):
            raise RuntimeError("required status-check app binding is invalid")
        if context != SELF_CHECK_NAME:
            app_bound.add((context, app_id))
    app_bound_names = {context for context, _ in app_bound}
    legacy = {
        (context, None)
        for context in contexts
        if context != SELF_CHECK_NAME and context not in app_bound_names
    }
    return legacy | app_bound


def _parse_check_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stable_check_run_id(raw: dict[str, object]) -> int | None:
    for key in ("id", "run_id"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    check_suite = raw.get("check_suite")
    if isinstance(check_suite, dict):
        value = check_suite.get("id")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _check_run_order_key(
    raw: dict[str, object],
) -> tuple[bool, datetime, bool, datetime, bool, int, str]:
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    completed_at = _parse_check_timestamp(raw.get("completed_at"))
    created_at = _parse_check_timestamp(raw.get("created_at"))
    run_id = _stable_check_run_id(raw)
    return (
        completed_at is not None,
        completed_at or minimum,
        created_at is not None,
        created_at or minimum,
        run_id is not None,
        run_id if run_id is not None else -1,
        json.dumps(raw, sort_keys=True, separators=(",", ":")),
    )


def _check_states(head_sha: str) -> tuple[list[str], list[str], list[str]]:
    """Return failing, pending, and missing checks for the exact head SHA."""
    latest_by_key: dict[tuple[str, int | None], dict[str, object]] = {}
    page = 1
    while True:
        payload = json.loads(
            _gh(
                "api",
                f"repos/{REPO}/commits/{head_sha}/check-runs?per_page=100&page={page}",
            )
        )
        runs = payload.get("check_runs")
        if not isinstance(runs, list):
            raise RuntimeError("check-run census is unavailable")
        for raw in runs:
            if not isinstance(raw, dict):
                raise RuntimeError("check-run census contains an invalid row")
            name = raw.get("name")
            if not isinstance(name, str) or not name or name == SELF_CHECK_NAME:
                continue
            app = raw.get("app")
            app_id = app.get("id") if isinstance(app, dict) else None
            if app_id is not None and not isinstance(app_id, int):
                raise RuntimeError("check-run app identity is invalid")
            key = (name, app_id)
            current = latest_by_key.get(key)
            if current is None or _check_run_order_key(raw) > _check_run_order_key(
                current
            ):
                latest_by_key[key] = raw
        if len(runs) < 100:
            break
        page += 1

    required = _required_check_contexts()
    missing_legacy_runs = {
        context
        for context, app_id in required
        if app_id is None and not any(name == context for name, _ in latest_by_key)
    }
    latest_status_by_context: dict[str, dict[str, object]] = {}
    if missing_legacy_runs:
        page = 1
        while True:
            payload = json.loads(
                _gh(
                    "api",
                    f"repos/{REPO}/commits/{head_sha}/statuses?"
                    f"per_page=100&page={page}",
                )
            )
            if not isinstance(payload, list):
                raise RuntimeError("commit-status census is unavailable")
            for raw in payload:
                if not isinstance(raw, dict):
                    raise RuntimeError("commit-status census contains an invalid row")
                context = raw.get("context")
                if not isinstance(context, str) or not context:
                    raise RuntimeError("commit-status context is invalid")
                if context in missing_legacy_runs:
                    latest_status_by_context.setdefault(context, raw)
            if len(payload) < 100:
                break
            page += 1
    failing: set[str] = set()
    pending: set[str] = set()
    for (name, _), item in latest_by_key.items():
        if item.get("status") != "completed":
            pending.add(name)
        elif item.get("conclusion") != "success":
            failing.add(name)
    for context, item in latest_status_by_context.items():
        state = item.get("state")
        if state == "pending":
            pending.add(context)
        elif state != "success":
            failing.add(context)
    missing: list[str] = []
    for context, app_id in sorted(
        required, key=lambda item: (item[0], -1 if item[1] is None else item[1])
    ):
        present = (
            any(name == context for name, _ in latest_by_key)
            or context in latest_status_by_context
            if app_id is None
            else (context, app_id) in latest_by_key
        )
        if not present:
            missing.append(context if app_id is None else f"{context}@app:{app_id}")
    return sorted(failing), sorted(pending), missing


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

    if not isinstance(head, str) or not head:
        raise RuntimeError("pull request head SHA is unavailable")
    failing, pending, missing = _check_states(head)
    if failing:
        failures.append(f"failing checks: {', '.join(sorted(failing))}")
    if pending:
        failures.append(f"pending checks: {', '.join(sorted(pending))}")
    if missing:
        failures.append(f"missing required checks: {', '.join(missing)}")

    # CLEAN/HAS_HOOKS are unconditionally acceptable. UNSTABLE is acceptable
    # only when nothing outside this check explains it (i.e., the instability
    # is our own prior runs); anything else was already reported above.
    state = pr.get("mergeStateStatus")
    stable = state in {"CLEAN", "HAS_HOOKS"}
    self_caused_unstable = (
        state == "UNSTABLE"
        and not failing
        and not pending
        and not missing
        and not unresolved
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
                "missing_required_checks": missing,
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
