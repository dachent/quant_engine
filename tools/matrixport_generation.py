"""Build or stage one hash-bound Matrixport/BIT partial generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from quant_engine.matrixport import build_generation, stage_generation_candidate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="verify without writes")
    build.add_argument("spec", type=Path)
    stage = commands.add_parser("stage", help="stage immutable partial generation")
    stage.add_argument("spec", type=Path)
    stage.add_argument("output_root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        catalog, manifest, catalog_bytes = build_generation(args.spec)
        result = {
            "catalog_size_bytes": len(catalog_bytes),
            "coverage_status": manifest.coverage.status.value,
            "dataset_count": len(catalog["datasets"]),  # type: ignore[arg-type]
            "generation_id": manifest.generation_id,
            "publication_eligible": False,
            "request_count": catalog["request_count"],
        }
    else:
        result = stage_generation_candidate(args.spec, args.output_root)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
