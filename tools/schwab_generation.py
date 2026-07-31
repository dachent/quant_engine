"""Build or stage one hash-bound, read-only Schwab generation candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from quant_engine.schwab import build_generation, stage_generation_candidate


def _summary(spec_path: Path) -> dict[str, object]:
    catalog, manifest, catalog_bytes = build_generation(spec_path)
    return {
        "account_count": len(catalog["accounts"]),  # type: ignore[arg-type]
        "catalog_size_bytes": len(catalog_bytes),
        "coverage_status": manifest.coverage.status.value,
        "generation_id": manifest.generation_id,
        "nav_count": len(catalog["nav_observations"]),  # type: ignore[arg-type]
        "position_count": len(catalog["positions"]),  # type: ignore[arg-type]
        "position_quantity_unavailable_count": catalog[
            "position_quantity_unavailable_count"
        ],
        "transaction_count": len(catalog["transactions"]),  # type: ignore[arg-type]
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="verify and build without writes")
    build.add_argument("spec", type=Path)
    stage = subparsers.add_parser("stage", help="create an immutable partial candidate")
    stage.add_argument("spec", type=Path)
    stage.add_argument("output_root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = (
        _summary(args.spec)
        if args.command == "build"
        else stage_generation_candidate(args.spec, args.output_root)
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
