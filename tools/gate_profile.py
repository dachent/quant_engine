"""Issue a reuse receipt and preview the data-only gate profile decision (default-off profile).

``issue`` runs AFTER a release is COMMITTED and post-release-verified: it runs the clean-room
restore, then writes the reuse receipt that lets the NEXT data-only release skip the replica
ceremony (``quant_engine.core.gate_profile``). ``select`` only prints the decision
``data_pipeline.py execute --commit --gate-profile data-only`` would take and writes nothing
else; it never commits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Sequence

import verify_release_restore
from quant_engine.core import (
    GATE_PROFILE_APPROVAL_KIND,
    GATE_PROFILE_ID,
    GATE_PROFILE_SCHEMA_VERSION,
    GateProfileError,
    canonical_json_bytes,
    gate_profile_tool_identity,
    issue_gate_profile_reuse_receipt,
    load_gate_profile_approval,
    load_pipeline_spec,
    read_current_release,
    seal_gate_profile_record,
    select_gate_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _issue(args: argparse.Namespace) -> int:
    spec = load_pipeline_spec(args.spec)
    manifest = read_current_release(
        spec.coordination_root,
        {root_id: spec.roots[root_id] for root_id in spec.release_generations},
    )
    log = args.clean_room.parent / f"{args.clean_room.name}.restore.log"
    args.clean_room.parent.mkdir(parents=True, exist_ok=True)
    with (
        log.open("w", encoding="utf-8") as handle,
        redirect_stdout(handle),
        redirect_stderr(handle),
    ):
        restore_rc = verify_release_restore.main(
            ["--spec", str(args.spec), "--clean-room", str(args.clean_room)]
        )
    receipt = issue_gate_profile_reuse_receipt(
        spec=spec,
        manifest=manifest,
        identity=gate_profile_tool_identity(REPO_ROOT),
        anchor=args.ceremony_receipt,
        previous_receipt=args.previous_reuse_receipt,
        post_verify_receipt=args.post_release_verify_receipt,
        clean_room_rc=restore_rc,
        clean_room_log_sha256=hashlib.sha256(log.read_bytes()).hexdigest(),
    )
    args.out.write_bytes(canonical_json_bytes(receipt))
    print(
        json.dumps(
            {"chain_depth": receipt["chain_depth"], "release_id": receipt["release_id"]}
        )
    )
    return 0


def _select(args: argparse.Namespace) -> int:
    decision = select_gate_profile(
        spec=load_pipeline_spec(args.spec),
        repo_root=REPO_ROOT,
        approval_path=args.approval,
        reuse_receipt_path=args.reuse_receipt,
    )
    print(json.dumps(decision.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0


def _seal_approval(args: argparse.Namespace) -> int:
    # Write this only after the owner's direct ruling; the record is tamper-evident, not
    # forgery-proof, like the replica receipt.
    body = {
        "approved_by": "owner",
        "approved_on": args.approved_on,
        "kind": GATE_PROFILE_APPROVAL_KIND,
        "max_consecutive_reuse": args.max_consecutive_reuse,
        "profile": GATE_PROFILE_ID,
        "schema_version": GATE_PROFILE_SCHEMA_VERSION,
        "statement": args.statement,
    }
    args.out.write_bytes(canonical_json_bytes(seal_gate_profile_record(body)))
    load_gate_profile_approval(args.out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("--spec", type=Path, required=True)
    issue.add_argument("--post-release-verify-receipt", type=Path, required=True)
    issue.add_argument("--clean-room", type=Path, required=True)
    issue.add_argument("--out", type=Path, required=True)
    issue.add_argument("--ceremony-receipt", type=Path)
    issue.add_argument("--previous-reuse-receipt", type=Path)
    select = commands.add_parser("select")
    select.add_argument("--spec", type=Path, required=True)
    select.add_argument("--approval", type=Path, required=True)
    select.add_argument("--reuse-receipt", type=Path)
    approval = commands.add_parser("seal-approval")
    approval.add_argument("--approved-on", required=True)
    approval.add_argument("--max-consecutive-reuse", type=int, required=True)
    approval.add_argument("--statement", required=True)
    approval.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    handlers = {"issue": _issue, "select": _select, "seal-approval": _seal_approval}
    try:
        return handlers[args.command](args)
    except (GateProfileError, OSError, RuntimeError, ValueError) as exc:
        print(f"gate-profile: FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
