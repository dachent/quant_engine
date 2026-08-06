"""Build or stage one hash-bound Matrixport/BIT partial generation."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Sequence

from quant_engine.matrixport import (
    build_generation,
    draft_generation_spec,
    stage_generation_candidate,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="verify without writes")
    build.add_argument("spec", type=Path)
    stage = commands.add_parser("stage", help="stage immutable partial generation")
    stage.add_argument("spec", type=Path)
    stage.add_argument("output_root", type=Path)
    draft = commands.add_parser(
        "draft-spec", help="draft a hash-bound spec from one verified export"
    )
    draft.add_argument("run_root", type=Path)
    draft.add_argument("as_of_date", type=date.fromisoformat)
    draft.add_argument("output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result: dict[str, object]
    if args.command == "draft-spec":
        content = draft_generation_spec(args.run_root, args.as_of_date)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists() and args.output.read_bytes() != content:
            raise ValueError("draft spec output already differs")
        if not args.output.exists():
            args.output.write_bytes(content)
        result = {
            "output": str(args.output.resolve()),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    elif args.command == "build":
        catalog, manifest, catalog_bytes = build_generation(args.spec)
        datasets = catalog["datasets"]
        if not isinstance(datasets, list):
            raise TypeError("generation catalog datasets must be an array")
        result = {
            "catalog_size_bytes": len(catalog_bytes),
            "coverage_status": manifest.coverage.status.value,
            "dataset_count": len(datasets),
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
