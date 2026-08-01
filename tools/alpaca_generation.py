"""Plan, build, publish, verify, and cut over the finite Alpaca baseline."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Sequence

from quant_engine.alpaca import (
    AlpacaGenerationError,
    build_generation,
    create_plan,
    cutover_source,
    publish_candidate,
    stage_candidate,
    verify_published,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("source_root", type=Path)
    plan.add_argument("data_root", type=Path)
    plan.add_argument("tracker_events", type=Path)
    plan.add_argument("--internal-account-id", required=True)
    plan.add_argument("--as-of-date", type=date.fromisoformat, required=True)
    plan.add_argument("--expected-source-file-count", type=int, default=23)
    for command in ("build", "stage"):
        child = commands.add_parser(command)
        child.add_argument("plan", type=Path)
    for command in ("publish", "verify", "cutover"):
        child = commands.add_parser(command)
        child.add_argument("plan", type=Path)
        child.add_argument("generation_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = create_plan(
                args.source_root,
                args.data_root,
                args.tracker_events,
                internal_account_id=args.internal_account_id,
                as_of_date=args.as_of_date,
                expected_source_file_count=args.expected_source_file_count,
            )
        elif args.command == "build":
            catalog, manifest, catalog_bytes = build_generation(args.plan)
            result = {
                "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
                "generation_id": manifest.generation_id,
                "nav_observation_count": len(catalog["nav_observations"]),  # type: ignore[arg-type]
                "state": "BUILT_AND_VERIFIED",
                "transaction_count": len(catalog["transactions"]),  # type: ignore[arg-type]
            }
        elif args.command == "stage":
            result = stage_candidate(args.plan)
        elif args.command == "publish":
            result = publish_candidate(args.plan, args.generation_id)
        elif args.command == "verify":
            result = verify_published(args.plan, args.generation_id)
        else:
            result = cutover_source(args.plan, args.generation_id)
    except (AlpacaGenerationError, OSError, ValueError) as exc:
        print(f"alpaca-generation: FAIL: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
