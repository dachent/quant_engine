"""G0 static gate: every fact a release run needs that can be read before any stage runs (#884).

One principle (Fable RULING-D 3.5): check each fact at the earliest moment it exists, with the consumer's own
code, and prove the check can fail. A row calls the driver's or core's own function (``Run.require_frozen_repo``,
``check_config_paths``, ``read_current_release``); it never restates their rule. Two outcomes gate: PASS and FAIL
(a row that cannot be checked is FAIL, never "not proven"). There is no declared exception and no release-specific
constant: everything comes from the run config.

Usage::

    python tools/release_preflight.py g0 --config run.json [--graph G]      # writes {cycle}/preflight/g0.json
    python tools/release_preflight.py retire --coordination-root D --release-id ID --ticket N --archive-dir A

``release_driver.py`` refuses a stage that declares ``needs_gate: ["g0"]`` without a PASS receipt bound to the same
run configuration and code identity. Run G0 again at lane entry and immediately before the commit: pointers move
between the first run and those moments (R3 refusal 15).

Not built here (named, so a green G0 is not read as "inputs are fine"): the consumer-contract gate G1, the product
measurements G2 and the spec-pair gate G3 (see COVERAGE).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import release_driver as driver

GIB = 1 << 30
RECEIPT_DIR = "preflight"
NON_TERMINAL = ("PREPARED", "COMMITTING", "ROLLING_BACK", "RECOVERY_FAILED")
RETIRED_DIR = "releases-retired"

DiskUsage = Callable[[Path], int]  # free bytes under a path


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


class Env:
    """The few things a test must replace: free disk space."""

    def __init__(self, free_bytes: DiskUsage = _free_bytes) -> None:
        self.free_bytes = free_bytes


Row = Callable[[driver.Run, Env], None]  # raises driver.Refusal with the reason


def _path(run: driver.Run, key: str) -> Path:
    if key not in run.values:
        raise driver.Refusal(f"run config lacks {key!r}")
    return Path(run.values[key])


def row_free_space(run: driver.Run, env: Env) -> None:
    """R3 refusal 18: C: filled mid-run. The floor is the run config's; none configured is a failure."""

    if "free_space_floor_gb" not in run.values:
        raise driver.Refusal("run config lacks free_space_floor_gb")
    floor = int(float(run.values["free_space_floor_gb"]) * GIB)
    low = []
    # the cycle may not exist yet: its nearest existing ancestor is the volume; a configured scratch root must exist
    probe = run.cycle
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if env.free_bytes(probe) < floor:
        low.append(str(run.cycle))
    for item in dict.fromkeys(map(str, run.config.get("scratch_roots", []))):
        if not Path(item).exists():
            raise driver.Refusal(f"scratch root {item} does not exist")
        if env.free_bytes(Path(item)) < floor:
            low.append(item)
    if low:
        raise driver.Refusal(
            f"free space under {sorted(set(low))} is below the {run.values['free_space_floor_gb']} GB floor"
        )


def row_no_live_stage(run: driver.Run, env: Env) -> None:
    """R3 refusals 20, 21: a scanner or stage working on the same trees. Nothing of this run may be alive."""

    driver.check_no_live_stage(run)


def row_frozen_repo(run: driver.Run, env: Env) -> None:
    """The repository is the run's code identity with a clean tree (the identity read retries once)."""

    run.require_frozen_repo()


def row_config_paths(run: driver.Run, env: Env) -> None:
    """R3 refusals 5, 24: pinned files hash to their pins; config paths exist and lie where the graph says; the
    evidence destination is outside the cycle; no configured root is a link."""

    driver.check_config_paths(run)
    if "evidence_dest" in run.values and driver.inside(
        Path(run.values["evidence_dest"]), run.cycle
    ):
        raise driver.Refusal(
            f"evidence_dest {run.values['evidence_dest']} lies inside the cycle {run.cycle}"
        )
    for key in ("cycle", "evidence_dest", "coordination_root", "source_workspace"):
        if key in run.values and driver.link_like(Path(run.values[key])):
            raise driver.Refusal(f"config {key}={run.values[key]} is a link")


def row_outside_workspace(run: driver.Run, env: Env) -> None:
    """R3 refusal 19: an input inside the real workspace normalizes differently in the replica (topology
    mismatch two hours in). Declared artifact inputs and pinned files stay outside the real workspace unless the
    run config lists them in ``replica_relative_ok``."""

    if "source_workspace" not in run.values:
        raise driver.Refusal("run config lacks source_workspace")
    workspace = Path(run.values["source_workspace"])
    allowed = {str(Path(item)) for item in run.config.get("replica_relative_ok", [])}
    inside = [
        item
        for item in [
            *map(str, run.config.get("artifact_inputs", [])),
            *dict(run.config.get("pinned_files", {})),
        ]
        if str(Path(item)) not in allowed and driver.inside(Path(item), workspace)
    ]
    if inside:
        raise driver.Refusal(
            f"inputs inside the real workspace {workspace}: {sorted(inside)[:3]} "
            "(move them outside, or name them in replica_relative_ok)"
        )


def row_pointer_graph(run: driver.Run, env: Env) -> None:
    """R3 refusal 15: CURRENT pointers moved after the previous commit. The consumer's own validation, not a
    copy of its rule: ``read_current_release`` raises ``committed pointer graph differs: <root>``."""

    from quant_engine.core import read_current_release

    roots = run.config.get("authority_roots")
    if not isinstance(roots, dict) or not roots:
        raise driver.Refusal("run config lacks authority_roots (root id to directory)")
    try:
        read_current_release(
            _path(run, "coordination_root"), {k: Path(v) for k, v in roots.items()}
        )
    except Exception as exc:  # the consumer's refusal text is the finding
        raise driver.Refusal(f"previous authority graph: {exc}") from exc


def non_terminal_releases(coordination_root: Path) -> list[str]:
    from quant_engine.core import read_release

    found = []
    releases = coordination_root / "releases"
    for item in sorted(releases.iterdir()) if releases.is_dir() else []:
        if not item.is_dir():
            continue
        try:
            _, journal = read_release(coordination_root, item.name)
        except Exception:  # an unreadable release directory is not a terminal one
            found.append(f"{item.name}:UNREADABLE")
            continue
        if str(journal.state) in NON_TERMINAL:
            found.append(f"{item.name}:{journal.state}")
    return found


def row_no_stale_prepared(run: driver.Run, env: Env) -> None:
    """No release left in a non-terminal state (16 PREPARED were found next to 12 COMMITTED). Retire them with
    ``retire`` (archive evidence first); nothing is declared away."""

    left = non_terminal_releases(_path(run, "coordination_root"))
    if left:
        raise driver.Refusal(
            f"{len(left)} release(s) not in a terminal state: {left[:3]} (archive and retire them)"
        )


def row_cutoff_stated(run: driver.Run, env: Env) -> None:
    """The cutoff of release A and of release B is stated in the config and the run's is one of them."""

    cutoffs = run.config.get("release_cutoffs")
    label = run.values.get("release_label")
    if not isinstance(cutoffs, dict) or not cutoffs:
        raise driver.Refusal("run config lacks release_cutoffs (label to cutoff)")
    if label not in cutoffs:
        raise driver.Refusal(f"release_label {label!r} is not in release_cutoffs")
    if run.values.get("requested_as_of") != cutoffs[label]:
        raise driver.Refusal(
            f"requested_as_of {run.values.get('requested_as_of')} is not the stated "
            f"cutoff {cutoffs[label]} of release {label}"
        )


def row_release_inputs(run: driver.Run, env: Env) -> None:
    """R3 refusals 5, 6, 16: the release-inputs root is bound (pin, cutoff, baseline bytes); and a declared
    strategy basis is passed to the validators, which only happens when the root is configured."""

    declaration = run.graph.get("paths", {}).get("release_inputs_strategy_basis")
    if "release_inputs_root" not in run.values:
        if declaration is None or run.offline:
            return
        raise driver.Refusal(
            "no release_inputs_root: the strategy-basis declaration would not reach the validators"
        )
    if run.release_inputs is None:
        raise driver.Refusal("release inputs are not bound")
    run.recheck_release_inputs()


ROWS: tuple[tuple[str, Row], ...] = (
    ("host:free-space", row_free_space),
    ("host:no-live-stage", row_no_live_stage),
    ("repo:frozen", row_frozen_repo),
    ("config:paths", row_config_paths),
    ("config:outside-workspace", row_outside_workspace),
    ("inputs:release-inputs", row_release_inputs),
    ("inputs:cutoff-stated", row_cutoff_stated),
    ("authority:pointer-graph", row_pointer_graph),
    ("authority:no-stale-prepared", row_no_stale_prepared),
)

# R3 refusal number (RULING-D section 2) -> where it is caught. "G0:<row>" is built here with a kill test in
# tests/portfolio_operations/test_release_preflight.py; "guard:<name>" is a tool guard with its own test;
# everything else is named, not built, and a green G0 does not cover it.
COVERAGE: dict[int, str] = {
    1: "G1:ledger-spec finality validators (not built)",
    2: "G1:ledger-spec finality validators (not built)",
    3: "G1:ledger-spec provider header (not built)",
    4: "G1:ledger-spec strategy evidence (not built)",
    5: "G0:config:paths",
    6: "G0:inputs:release-inputs",
    7: "G1:attended identity validation (not built)",
    8: "G1:sleeve-facts admission (not built)",
    9: "by design: the first calendar attempt",
    10: "G1:attribution contract (not built)",
    11: "G1:attribution contract (not built)",
    12: "G2:needs the build (not built)",
    13: "G1:finality evidence reader (not built)",
    14: "G0:freshness against the consumer's limit (not built)",
    15: "G0:authority:pointer-graph",
    16: "G0:inputs:release-inputs",
    17: "G2:payload size at candidate time (not built)",
    18: "G0:host:free-space",
    19: "G0:config:outside-workspace",
    20: "guard:git-identity-retry (a transient identity read only; a scanner is not detected)",
    21: "guard:git-identity-retry (a transient identity read only; a scanner is not detected)",
    22: "guard:require_checkout_is_spec_code",
    23: "guard:require_checkout_is_spec_code",
    24: "G0:config:paths",
}


def config_sha256(run: driver.Run) -> str:
    return hashlib.sha256(
        json.dumps(run.config, sort_keys=True).encode("utf-8")
    ).hexdigest()


def run_g0(run: driver.Run, env: Env | None = None) -> dict[str, Any]:
    env = env or Env()
    rows = []
    for row_id, fn in ROWS:
        try:
            fn(run, env)
            rows.append({"row": row_id, "status": "PASS", "detail": ""})
        except (driver.Refusal, OSError, ValueError, KeyError) as exc:
            rows.append({"row": row_id, "status": "FAIL", "detail": str(exc)[:400]})
    return {
        "kind": "release-preflight-g0-v1",
        "gate": "G0",
        "config_sha256": config_sha256(run),
        "code_commit": run.values["code_commit"],
        "git_retries": list(run.git_retries),
        "rows": rows,
        "status": "PASS" if all(r["status"] == "PASS" for r in rows) else "FAIL",
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def recheck(run: driver.Run, row_ids: Sequence[str], env: Env | None = None) -> None:
    """Run the named rows live; the first FAIL refuses (lane entry and the commit repeat the moving ones)."""

    env = env or Env()
    table = dict(ROWS)
    for row_id in row_ids:
        if row_id not in table:
            raise driver.Refusal(f"no preflight row {row_id!r}")
        try:
            table[row_id](run, env)
        except (OSError, ValueError, KeyError) as exc:
            raise driver.Refusal(f"preflight {row_id}: {exc}") from exc
        except driver.Refusal as exc:
            raise driver.Refusal(f"preflight {row_id}: {exc}") from exc


def receipt_path(run: driver.Run, gate: str) -> Path:
    return run.cycle / RECEIPT_DIR / f"{gate.lower()}.json"


def write_receipt(run: driver.Run, receipt: Mapping[str, Any]) -> Path:
    path = receipt_path(run, str(receipt["gate"]))
    driver.write_json(path, dict(receipt))
    return path


def verify_receipt(run: driver.Run, gate: str) -> None:
    """``attended`` (and the lanes) refuse without a PASS receipt for this config and code identity."""

    path = receipt_path(run, gate)
    if not path.is_file():
        raise driver.Refusal(
            f"gate {gate} has no receipt at {path}; run release_preflight.py {gate.lower()} first"
        )
    receipt = driver.read_json(path)
    rows = receipt.get("rows")
    if (
        receipt.get("kind") != "release-preflight-g0-v1"
        or not isinstance(rows, list)
        or not rows
    ):
        raise driver.Refusal(f"gate {gate} receipt is malformed (no rows)")
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(receipt["written_at"])
    except (KeyError, ValueError) as exc:
        raise driver.Refusal(f"gate {gate} receipt has no readable written_at") from exc
    limit = float(run.values.get("gate_receipt_max_age_hours", 24))
    if age.total_seconds() > limit * 3600:
        raise driver.Refusal(
            f"gate {gate} receipt is older than {limit:g} h; run it again"
        )
    if (
        receipt.get("config_sha256") != config_sha256(run)
        or receipt.get("code_commit") != run.values["code_commit"]
    ):
        raise driver.Refusal(
            f"gate {gate} receipt is for another run configuration or code identity"
        )
    failed = [r.get("row") for r in rows if r.get("status") != "PASS"]
    if receipt.get("status") != "PASS" or failed:
        raise driver.Refusal(f"gate {gate} receipt is FAIL: {failed[:4]}")


def retire_release(
    coordination_root: Path, release_id: str, ticket: str, archive_dir: Path
) -> Path:
    """Archive a non-terminal release's evidence, then move it aside (never delete): ticket-backed retirement.

    The authority release is never retired. The archive copy is hashed against the original before the original
    moves, and the retirement record names the ticket, the state and the tree digest.
    """

    from quant_engine.core import read_release

    source = coordination_root / "releases" / release_id
    _, journal = read_release(coordination_root, release_id)
    if str(journal.state) not in NON_TERMINAL:
        raise driver.Refusal(
            f"release {release_id[:12]} is {journal.state}: only a non-terminal release is retired"
        )
    authority = coordination_root / "CURRENT_RELEASE.json"
    if (
        authority.is_file()
        and driver.read_json(authority).get("release_id") == release_id
    ):
        raise driver.Refusal("the authority release is never retired")
    destination = coordination_root / RETIRED_DIR / release_id
    if destination.exists():
        raise driver.Refusal(f"{destination} already exists")
    if not ticket.strip():
        raise driver.Refusal("retirement needs a ticket")
    target = archive_dir / release_id
    if target.exists():
        raise driver.Refusal(f"archive {target} already exists")
    shutil.copytree(source, target)
    digest = driver.path_digest(source)
    if driver.path_digest(target) != digest:
        shutil.rmtree(target)
        raise driver.Refusal("archived copy does not match the release directory")
    record = {
        "release_id": release_id,
        "state": str(journal.state),
        "ticket": ticket,
        "tree_sha256": digest,
        "retired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    driver.write_json(archive_dir / f"{release_id}.retirement.json", record)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    g0 = sub.add_parser("g0")
    g0.add_argument("--config", type=Path, required=True)
    g0.add_argument("--graph", type=Path)
    retire = sub.add_parser("retire")
    retire.add_argument("--coordination-root", type=Path, required=True)
    retire.add_argument("--release-id", required=True)
    retire.add_argument("--ticket", required=True)
    retire.add_argument("--archive-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "retire":
            moved = retire_release(
                args.coordination_root,
                args.release_id,
                args.ticket,
                args.archive_dir,
            )
            print(f"retired {args.release_id[:12]} to {moved}")
            return 0
        run = driver.load_run(args.config, args.graph)
        receipt = run_g0(run)
        path = write_receipt(run, receipt)
        for row in receipt["rows"]:
            print(f"{row['status']:4} {row['row']} {row['detail']}")
        print(f"G0 {receipt['status']}: {path}")
        return 0 if receipt["status"] == "PASS" else 1
    except driver.Refusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
