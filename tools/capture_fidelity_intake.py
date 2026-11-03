"""Offline, governed intake of an owner-exported Fidelity activity file.

There is no network or login code here.  The owner exports the file; this tool
writes the request manifest, stages the received bytes create-only, normalizes
them, and verifies the result.  Output is aggregate JSON only.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from typing import Sequence

from quant_engine.fidelity import (
    IntakeError,
    IntakeVerificationError,
    catalog_rows,
    normalize,
    stage_raw,
    verify_intake,
    write_request,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog", help="print the accepted report-type catalog")
    request = sub.add_parser("request", help="write the owner request manifest")
    request.add_argument("staging_root", type=Path)
    request.add_argument("--evidence", type=Path, required=True)
    request.add_argument("--report-type", required=True)
    request.add_argument("--window-start", type=date.fromisoformat, required=True)
    request.add_argument("--window-end", type=date.fromisoformat, required=True)
    request.add_argument("--now", help="RFC3339 override for the governance check")
    stage = sub.add_parser("stage-raw", help="stage owner files as received")
    stage.add_argument("staging_root", type=Path)
    stage.add_argument("request_id")
    stage.add_argument("files", nargs="+", type=Path)
    norm = sub.add_parser("normalize", help="normalize staged raw files")
    norm.add_argument("staging_root", type=Path)
    norm.add_argument("request_id")
    norm.add_argument("--crosswalk", type=Path, required=True)
    verify = sub.add_parser("verify", help="independently verify the intake")
    verify.add_argument("staging_root", type=Path)
    verify.add_argument("request_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "catalog":
            result: object = catalog_rows()
        elif args.command == "request":
            now = (
                datetime.fromisoformat(args.now.replace("Z", "+00:00"))
                if args.now
                else datetime.now(timezone.utc)
            )
            result = write_request(
                args.staging_root,
                evidence_path=args.evidence,
                report_type_id=args.report_type,
                window_start=args.window_start,
                window_end=args.window_end,
                now=now,
            )
        elif args.command == "stage-raw":
            result = stage_raw(args.staging_root, args.request_id, args.files)
        elif args.command == "normalize":
            result = normalize(args.staging_root, args.request_id, args.crosswalk)
        else:
            result = verify_intake(args.staging_root, args.request_id)
    except IntakeVerificationError as exc:
        print(json.dumps({"reasons": exc.reasons, "status": "FAILED"}), file=sys.stderr)
        return 1
    except IntakeError as exc:
        print(json.dumps({"reason": str(exc), "status": "REFUSED"}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
