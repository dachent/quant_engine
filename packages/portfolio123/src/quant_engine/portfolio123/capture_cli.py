"""Offline command line for owner-supplied Portfolio123 exports.

No command logs in, reaches the network, or calls the API.  Output is one line of
canonical JSON holding only counts, hashes, and flags; a refusal prints a stable
reason code and exits with status 2.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime
import json
from pathlib import Path
import sys

from quant_engine.portfolio123.capture_catalog import (
    FORM_FILE,
    FORM_HAR,
    CATALOG,
    CaptureRefusal,
    catalog_sha256,
)
from quant_engine.portfolio123.capture_intake import (
    StageRequest,
    inventory_har,
    stage_run,
    verify_run,
)


def _time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("expected an RFC3339 timestamp") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="p123-capture", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog", help="print the capture catalog identity")
    for name, form in (("stage-har", FORM_HAR), ("stage-file", FORM_FILE)):
        stage = commands.add_parser(name)
        stage.add_argument("--staging-root", type=Path, required=True)
        stage.add_argument("--run-id", required=True)
        stage.add_argument("--input", type=Path, required=True, dest="source")
        stage.add_argument("--captured-at", type=_time, required=True)
        stage.add_argument(
            "--capability",
            action="append",
            required=True,
            dest="capabilities",
            help="repeatable for HAR; exactly one for a file",
        )
        if form == FORM_HAR:
            stage.add_argument("--session-expires-at", type=_time, required=True)
            stage.add_argument("--allow-partial", action="store_true")
    inventory = commands.add_parser("inventory-har", help="route templates only")
    inventory.add_argument("--staging-root", type=Path, required=True)
    inventory.add_argument("--run-id", required=True)
    inventory.add_argument("--input", type=Path, required=True, dest="source")
    inventory.add_argument(
        "--reveal-segments",
        action="store_true",
        help="keep unknown alphabetic path words (may disclose slugs)",
    )
    verify = commands.add_parser("verify")
    verify.add_argument("--staging-root", type=Path, required=True)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--expect-manifest-sha256")
    verify.add_argument("--allow-partial", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "catalog":
            result: dict[str, object] = {
                "catalog_sha256": catalog_sha256(),
                "capabilities": sorted(item.capability for item in CATALOG),
                "verdicts": {
                    item.capability: item.verdict
                    for item in sorted(CATALOG, key=lambda row: row.capability)
                },
            }
        elif args.command == "inventory-har":
            result = inventory_har(
                args.staging_root,
                args.run_id,
                args.source,
                reveal_segments=args.reveal_segments,
            )
        elif args.command in {"stage-har", "stage-file"}:
            form = FORM_HAR if args.command == "stage-har" else FORM_FILE
            result = stage_run(
                StageRequest(
                    staging_root=args.staging_root,
                    run_id=args.run_id,
                    form=form,
                    source=args.source,
                    capabilities=tuple(args.capabilities),
                    captured_at=args.captured_at,
                    session_expires_at=getattr(args, "session_expires_at", None),
                    allow_partial=getattr(args, "allow_partial", False),
                )
            ).to_dict()
        else:
            result = verify_run(
                args.staging_root,
                args.run_id,
                expect_manifest_sha256=args.expect_manifest_sha256,
                require_complete=not args.allow_partial,
            ).to_dict()
    except CaptureRefusal as refusal:
        print(f"REFUSED {refusal.code}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
