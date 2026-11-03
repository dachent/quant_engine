"""Plan, execute, resume, and inspect an explicit-ID data release DAG."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from quant_engine.core import (
    GateProfileError,
    ReplicaGateError,
    is_replica_spec,
    verify_replica_receipt,
    PipelineExecutionError,
    canonical_json_bytes,
    PipelineUnknownOutcomeError,
    PipelineValidationError,
    execute_pipeline,
    inventory_generation,
    load_pipeline_spec,
    pipeline_status,
    plan_pipeline,
    recover_pipeline,
    select_gate_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "execute", "status", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--spec", type=Path, required=True)
        if name == "execute":
            command.add_argument("--commit", action="store_true")
            command.add_argument("--commit-authorization", type=Path)
            # A real-roots commit presents the receipt of a passed replica ceremony
            # (tools/release_replica.py ceremony); a spec under a replica root marker
            # (replica_gate.REPLICA_MARKER_NAME) is the replica itself and needs none.
            command.add_argument("--replica-receipt", type=Path)
            command.add_argument("--allow-external-read", action="store_true")
            # Default OFF. The data-only gate profile (core/gate_profile.py) replaces the
            # replica receipt check only with an owner approval record and a reuse receipt;
            # any failed condition falls back to the full replica receipt check above.
            command.add_argument("--gate-profile", choices=("data-only",))
            command.add_argument("--gate-profile-approval", type=Path)
            command.add_argument("--reuse-receipt", type=Path)
            # Deliberately no CLI escape hatch for the release-validation
            # gate: a flag on the primary publish command is a friction-free
            # bypass that leaves no trail (journal, manifest, execute.json
            # all look like a normal commit). `commit_release`'s
            # `skip_release_validation` stays available as a Python-level
            # parameter for legitimate non-CLI callers that justify it inline
            # (rehearsal.py calls the API directly; see its 3 call sites).
        elif name == "recover":
            command.add_argument("--confirm-stale-lock", action="store_true")
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--generation-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inventory":
            result = inventory_generation(args.generation_dir)
        else:
            spec = load_pipeline_spec(args.spec)
            if args.command == "plan":
                result = plan_pipeline(spec)
            elif args.command == "status":
                result = pipeline_status(spec)
            elif args.command == "recover":
                result = recover_pipeline(
                    spec, clear_stale_lock=args.confirm_stale_lock
                )
            else:
                if args.commit and not is_replica_spec(spec):
                    if args.gate_profile is None:
                        verify_replica_receipt(args.replica_receipt, spec)
                    else:
                        decision = select_gate_profile(
                            spec=spec,
                            repo_root=REPO_ROOT,
                            approval_path=args.gate_profile_approval,
                            reuse_receipt_path=args.reuse_receipt,
                        )
                        (args.spec.parent / "gate-profile-decision.json").write_bytes(
                            canonical_json_bytes(decision.to_dict())
                        )
                        if decision.profile == "FULL":
                            print(
                                "data-pipeline: gate profile fell back to the full gates: "
                                + "; ".join(decision.reasons),
                                file=sys.stderr,
                            )
                            verify_replica_receipt(args.replica_receipt, spec)
                result = execute_pipeline(
                    spec,
                    commit=args.commit,
                    commit_authorization=args.commit_authorization,
                    allow_external_read=args.allow_external_read,
                )
    except (
        OSError,
        PipelineExecutionError,
        PipelineUnknownOutcomeError,
        PipelineValidationError,
        GateProfileError,
        ReplicaGateError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"data-pipeline: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    financial_mvp = result.get("financial_mvp")
    if (
        args.command in {"plan", "status"}
        and isinstance(financial_mvp, dict)
        and financial_mvp.get("ready_for_execution") is False
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
