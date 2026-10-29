"""Keep one last-good retained-cache base across release cycles, and measure reuse.

Every attended run starts from ``--retained-cache-base`` and writes its own overlay
(hard links to the base plus new files). The decomposition index, the decomposition
training cache and the financial replay receipts in it are content-keyed, so a verified
overlay from the previous pass can seed the next pass even at a new commit: a miss is
solved, never refused, and a key that includes a changed producer simply misses.

``promote``  hard-link a PASSED run's overlay into a new sealed base. The source commit is
             read from the run's own vertical recipe, never typed.
``verify``   prove a base is byte-for-byte what ``promote`` recorded (the attended runner
             also does this before it links any overlay to a promoted base).
``report``   after a run: per cache kind, entries inherited from the base vs new, and
             whether the producers changed between the base commit and HEAD (the caller
             passes the controller graph's output for base..HEAD). Exit 4 when producers of
             a cached stage changed yet nothing of that stage was computed: that means an
             undeclared producer dependency, so the result must not be trusted.

Nothing here changes what binds the frozen commit: the base is only an input cache.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from quant_engine.core import (
    CacheBaseError,
    iter_cache_files,
    promote_cache_base,
    read_cache_base_provenance,
    verify_cache_base,
)

OVERLAY_MARKER = "retained-cache-overlay.json"
RUN_STATE = "attended-run-state.json"
RECIPE = Path("preparation") / "release-recipe.json"
EXIT_SUSPICIOUS_REUSE = 4
# Content-keyed cache kinds (first path component under the base) and the controller
# stage whose producers key them.
KIND_STAGE = {
    ".decomposition-index": "analytics",
    ".decomposition-training": "analytics",
    "financial-replay-receipts": "analytics",
}


def _json(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CacheBaseError(f"{what} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise CacheBaseError(f"{what} root must be an object: {path}")
    return value


def source_commit_of(run_root: Path, overlay: Path) -> str:
    """The commit the run was frozen at, from the run itself; refuses an unfinished run
    or an overlay that belongs to another run."""

    state = _json(run_root / RUN_STATE, "attended run state").get("state")
    if state != "PASSED":
        raise CacheBaseError(f"run is not PASSED (state {state!r}): {run_root}")
    marker = _json(overlay / OVERLAY_MARKER, "overlay marker")
    if Path(str(marker.get("run_root"))).resolve() != run_root.resolve():
        raise CacheBaseError("overlay belongs to a different run root")
    commit = _json(run_root / RECIPE, "vertical recipe").get("release_commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise CacheBaseError("run recipe carries no 40-hex release_commit")
    return commit


def reuse_report(base: Path, overlay: Path) -> dict[str, Any]:
    """Per first-level cache kind: files shared with the base (inherited) vs new."""

    base_inodes = {(i.st_dev, i.st_ino) for _r, _p, i in iter_cache_files(base)}
    kinds: dict[str, dict[str, int]] = {}
    for rel, _path, info in iter_cache_files(overlay):
        if rel == OVERLAY_MARKER:
            continue
        kind = rel.split("/", 1)[0]
        row = kinds.setdefault(kind, {"inherited": 0, "new": 0})
        shared = (info.st_dev, info.st_ino) in base_inodes
        row["inherited" if shared else "new"] += 1
    return {"kinds": dict(sorted(kinds.items()))}


def _affected_stages(graph_output: Path) -> list[str]:
    """``affected_stages`` of ``portfolio_controller_graph.py --changed-from <base commit>
    --changed-to HEAD`` (stdout saved to a file by the caller; this tool runs no tools)."""

    stages = _json(graph_output, "controller graph output").get("affected_stages")
    if not isinstance(stages, list):
        raise CacheBaseError("controller graph output has no affected_stages")
    return [str(s) for s in stages]


def report(
    base: Path, overlay: Path, graph_output: Path, head: str
) -> tuple[dict[str, Any], int]:
    record = read_cache_base_provenance(base)
    result = reuse_report(base, overlay)
    changed = _affected_stages(graph_output)
    result.update(
        {
            "affected_stages_since_base": changed,
            "base_commit": record["source_commit"],
            "head": head,
        }
    )
    suspicious = []
    for stage in sorted(set(KIND_STAGE.values()) & set(changed)):
        rows = [
            result["kinds"][k]
            for k, s in KIND_STAGE.items()
            if s == stage and k in result["kinds"]
        ]
        new = sum(r["new"] for r in rows)
        if rows and new == 0 and sum(r["inherited"] for r in rows) > 0:
            suspicious.append(stage)
    result["suspicious_reuse"] = suspicious
    return result, (EXIT_SUSPICIOUS_REUSE if suspicious else 0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("promote")
    p.add_argument("--overlay", type=Path, required=True)
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--run-root", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--base", type=Path, required=True)
    r = sub.add_parser("report")
    r.add_argument("--base", type=Path, required=True)
    r.add_argument("--overlay", type=Path, required=True)
    r.add_argument("--graph-output", type=Path, required=True)
    r.add_argument("--head", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    try:
        if a.command == "promote":
            commit = source_commit_of(a.run_root, a.overlay)
            out: dict[str, Any] = promote_cache_base(a.overlay, a.base, commit)
            code = 0
        elif a.command == "verify":
            out, code = verify_cache_base(a.base), 0
        else:
            out, code = report(a.base, a.overlay, a.graph_output, a.head)
    except (CacheBaseError, OSError, ValueError) as exc:
        print(f"release-cache-base: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
