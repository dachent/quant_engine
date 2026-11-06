"""Plan, publish, verify, and cut over Portfolio123 local data."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from typing import Sequence

from quant_engine.portfolio123 import (
    create_cutover_plan,
    cutover_sources,
    publish_candidate,
    stage_candidate,
    verify_published,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("legacy_root", type=Path)
    plan.add_argument("har_root", type=Path)
    plan.add_argument("data_root", type=Path)
    plan.add_argument("--as-of-date", type=date.fromisoformat, required=True)
    plan.add_argument(
        "--transaction-capture",
        type=Path,
        action="append",
        default=[],
        help="capture dir: capture-manifest.json + raw/strategy_<id>_transactions.csv",
    )
    plan.add_argument(
        "--expected-book",
        action="append",
        default=[],
        help="book ID that must have a transaction capture file (repeatable)",
    )
    plan.add_argument(
        "--previous-generation-id",
        help="generation the transaction gate compares with (default: CURRENT)",
    )
    plan.add_argument(
        "--supersede-book",
        action="append",
        default=[],
        metavar="ID=REASON",
        help="waive the drop gate for one captured book, recorded in the catalog",
    )
    stage = commands.add_parser("stage")
    stage.add_argument("plan", type=Path)
    for command in (
        commands.add_parser("publish"),
        commands.add_parser("verify"),
        commands.add_parser("cutover"),
    ):
        command.add_argument("plan", type=Path)
        command.add_argument("generation_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "plan":
        supersede: dict[str, str] = {}
        for item in args.supersede_book:
            book, _, reason = item.partition("=")
            if book in supersede or not reason:
                parser.error("--supersede-book needs a unique ID=REASON")
            supersede[book] = reason
        result = create_cutover_plan(
            args.legacy_root,
            args.har_root,
            args.data_root,
            as_of_date=args.as_of_date,
            transaction_captures=args.transaction_capture,
            expected_books=args.expected_book,
            previous_generation_id=args.previous_generation_id,
            supersede_books=supersede,
        )
    elif args.command == "stage":
        result = stage_candidate(args.plan)
    elif args.command == "publish":
        result = publish_candidate(args.plan, args.generation_id)
    elif args.command == "verify":
        result = verify_published(args.plan, args.generation_id)
    else:
        result = cutover_sources(args.plan, args.generation_id)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
