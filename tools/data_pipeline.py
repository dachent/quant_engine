"""Plan, execute, resume, and inspect an explicit-ID data release DAG."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from quant_engine.core import (
    PipelineExecutionError,
    PipelineUnknownOutcomeError,
    PipelineValidationError,
    execute_pipeline,
    inventory_generation,
    load_pipeline_spec,
    pipeline_status,
    plan_pipeline,
    recover_pipeline,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "execute", "status", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--spec", type=Path, required=True)
        if name == "execute":
            command.add_argument("--commit", action="store_true")
            command.add_argument("--allow-external-read", action="store_true")
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
                result = execute_pipeline(
                    spec,
                    commit=args.commit,
                    allow_external_read=args.allow_external_read,
                )
    except (
        OSError,
        PipelineExecutionError,
        PipelineUnknownOutcomeError,
        PipelineValidationError,
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
