"""Build or stage one hash-bound, read-only Schwab generation candidate."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from typing import Sequence

from quant_engine.schwab import (
    build_generation,
    publish_generation_candidate,
    read_holdings_v2_current_snapshot,
    read_web_current_snapshot,
    stage_generation_candidate,
    write_current_snapshot,
)
from quant_engine.schwab.generation import _crosswalk


def _summary(spec_path: Path) -> dict[str, object]:
    catalog, manifest, catalog_bytes = build_generation(spec_path)
    return {
        "account_count": len(catalog["accounts"]),  # type: ignore[arg-type]
        "catalog_size_bytes": len(catalog_bytes),
        "coverage_status": manifest.coverage.status.value,
        "current_account_count": len(catalog.get("current_accounts", [])),  # type: ignore[arg-type]
        "generation_id": manifest.generation_id,
        "nav_count": len(catalog["nav_observations"]),  # type: ignore[arg-type]
        "position_count": len(catalog["positions"]),  # type: ignore[arg-type]
        "position_quantity_unavailable_count": catalog[
            "position_quantity_unavailable_count"
        ],
        "transaction_count": len(catalog["transactions"]),  # type: ignore[arg-type]
    }


def _web_current(
    raw_response: Path, spec_path: Path, output: Path
) -> dict[str, object]:
    raw = json.loads(raw_response.read_text(encoding="utf-8-sig"))
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    crosswalk = {
        str(item["external_account_id"]): str(item["internal_account_id"])
        for item in spec["crosswalk"]
    }
    snapshot = read_web_current_snapshot(raw, crosswalk=crosswalk)
    digest, size = write_current_snapshot(snapshot, output)
    return {
        "account_count": len(snapshot.accounts),
        "observed_at": snapshot.observed_at.isoformat(),
        "position_count": len(snapshot.positions),
        "sha256": digest,
        "size_bytes": size,
        "sweep_vehicle_count": snapshot.sweep_vehicle_count,
        "transport": snapshot.transport,
    }


def _holdings_v2_current(
    raw_response: Path, spec_path: Path, receipt_path: Path, output: Path
) -> dict[str, object]:
    raw = json.loads(raw_response.read_text(encoding="utf-8-sig"))
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    if not isinstance(spec, dict):
        raise ValueError("generation spec must be an object")
    try:
        generation_date = date.fromisoformat(str(spec["as_of_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("generation spec as_of_date is invalid") from exc
    crosswalk, _ = _crosswalk(spec.get("crosswalk"))
    if not isinstance(receipt, dict):
        raise ValueError("capture receipt must be an object")
    snapshot = read_holdings_v2_current_snapshot(
        raw,
        crosswalk=crosswalk,
        captured_at=receipt.get("captured_at_utc"),
    )
    if snapshot.observed_at.date() != generation_date:
        raise ValueError("capture receipt UTC date differs from generation as_of_date")
    digest, size = write_current_snapshot(snapshot, output)
    return {
        "account_count": len(snapshot.accounts),
        "observed_at": snapshot.observed_at.isoformat(),
        "position_count": len(snapshot.positions),
        "sha256": digest,
        "size_bytes": size,
        "sweep_vehicle_count": snapshot.sweep_vehicle_count,
        "transport": snapshot.transport,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="verify and build without writes")
    build.add_argument("spec", type=Path)
    normalize = subparsers.add_parser(
        "normalize-web-current",
        help="sanitize one reviewed all-accounts web position response",
    )
    normalize.add_argument("raw_response", type=Path)
    normalize.add_argument("spec", type=Path)
    normalize.add_argument("output", type=Path)
    holdings_v2 = subparsers.add_parser(
        "normalize-holdings-v2",
        help="sanitize one retained Holdings-V2 response using its capture receipt",
    )
    holdings_v2.add_argument("raw_response", type=Path)
    holdings_v2.add_argument("spec", type=Path)
    holdings_v2.add_argument("capture_receipt", type=Path)
    holdings_v2.add_argument("output", type=Path)
    stage = subparsers.add_parser("stage", help="create an immutable candidate")
    stage.add_argument("spec", type=Path)
    stage.add_argument("output_root", type=Path)
    publish = subparsers.add_parser(
        "publish", help="rebuild and publish one exact accepted generation"
    )
    publish.add_argument("spec", type=Path)
    publish.add_argument("output_root", type=Path)
    publish.add_argument("generation_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        result = _summary(args.spec)
    elif args.command == "normalize-web-current":
        result = _web_current(args.raw_response, args.spec, args.output)
    elif args.command == "normalize-holdings-v2":
        result = _holdings_v2_current(
            args.raw_response, args.spec, args.capture_receipt, args.output
        )
    elif args.command == "stage":
        result = stage_generation_candidate(args.spec, args.output_root)
    else:
        result = publish_generation_candidate(
            args.spec, args.output_root, args.generation_id
        )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
