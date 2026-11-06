"""Plan, publish, verify, and cut over Portfolio123 local data."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from typing import Any, Sequence

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
    plan.add_argument(
        "--no-transactions-book",
        action="append",
        default=[],
        metavar="ID=REASON",
        help="book with no transaction export (e.g. a designer model), recorded with its reason",
    )
    plan.add_argument(
        "--identity-correction",
        type=Path,
        help="JSON {previous_generation_id, books: {ID: {...}}} proving the predecessor held "
        "another strategy's rows under that id",
    )
    plan.add_argument(
        "--untrusted-har-id",
        action="append",
        default=[],
        metavar="ID=REASON",
        help="HAR download id not recaptured: its downloads/ rows are dropped; the reason "
        "states the id's downstream seed role",
    )
    plan.add_argument(
        "--identity-decisions",
        type=Path,
        action="append",
        default=[],
        help="book-identity-decisions.json of a capture (tools/p123_book_identity_check.py)",
    )
    plan.add_argument(
        "--accept-identity-review",
        action="append",
        default=[],
        metavar="ID=REASON",
        help="accept a REVIEW identity decision for one captured book with a reason",
    )
    plan.add_argument(
        "--book-inventory",
        type=Path,
        help="frozen inventory JSON {books: {ID: {role, route: capture|exempt|untrusted, "
        "reason}}}: the only source of the expected, exempt and untrusted books",
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


def _pairs(
    parser: argparse.ArgumentParser, items: list[str], flag: str
) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        book, _, reason = item.partition("=")
        if book in result or not reason:
            parser.error(f"{flag} needs a unique ID=REASON")
        result[book] = reason
    return result


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
        exempt: dict[str, str] = {}
        for item in args.no_transactions_book:
            book, _, reason = item.partition("=")
            if book in exempt or not reason:
                parser.error("--no-transactions-book needs a unique ID=REASON")
            exempt[book] = reason
        untrusted = _pairs(parser, args.untrusted_har_id, "--untrusted-har-id")
        accepted = _pairs(
            parser, args.accept_identity_review, "--accept-identity-review"
        )
        decisions = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in args.identity_decisions
        ]
        inventory = (
            json.loads(args.book_inventory.read_text(encoding="utf-8"))["books"]
            if args.book_inventory is not None
            else None
        )
        corrections: dict[str, dict[str, Any]] = {}
        if args.identity_correction is not None:
            record = json.loads(args.identity_correction.read_text(encoding="utf-8"))
            for book, fields in record["books"].items():
                corrections[book] = {
                    **fields,
                    "previous_generation_id": record["previous_generation_id"],
                }
        result = create_cutover_plan(
            args.legacy_root,
            args.har_root,
            args.data_root,
            as_of_date=args.as_of_date,
            transaction_captures=args.transaction_capture,
            expected_books=args.expected_book,
            previous_generation_id=args.previous_generation_id,
            supersede_books=supersede,
            transaction_exempt_books=exempt,
            identity_corrections=corrections,
            book_inventory=inventory,
            untrusted_har_ids=untrusted,
            identity_decisions=decisions,
            identity_review_accepted=accepted,
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
