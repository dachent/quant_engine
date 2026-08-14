"""Build, stage, or publish one hash-bound Matrixport/BIT generation."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Sequence

from quant_engine.core import (
    CoverageStatus,
    PublicationRejectedError,
    publish_generation,
)
from quant_engine.matrixport import (
    MatrixportGenerationError,
    build_generation,
    draft_generation_spec,
    stage_generation_candidate,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="verify without writes")
    build.add_argument("spec", type=Path)
    stage = commands.add_parser("stage", help="stage immutable generation")
    stage.add_argument("spec", type=Path)
    stage.add_argument("output_root", type=Path)
    publish = commands.add_parser(
        "publish", help="make one complete, verified generation current authority"
    )
    publish.add_argument("spec", type=Path)
    publish.add_argument("output_root", type=Path)
    publish.add_argument("generation_id")
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
    try:
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
            eligible = (
                manifest.coverage.status
                is CoverageStatus.COMPLETE_WITHIN_DECLARED_BOUNDARY
            )
            result = {
                "catalog_size_bytes": len(catalog_bytes),
                "coverage_status": manifest.coverage.status.value,
                "dataset_count": len(datasets),
                "generation_id": manifest.generation_id,
                "publication_eligible": eligible,
                "request_count": catalog["request_count"],
            }
            if manifest.coverage.undeclared_gaps:
                result["undeclared_gaps"] = list(manifest.coverage.undeclared_gaps)
            if manifest.coverage.stale_declarations:
                result["stale_declarations"] = list(
                    manifest.coverage.stale_declarations
                )
        elif args.command == "stage":
            result = stage_generation_candidate(args.spec, args.output_root)
        elif args.command == "publish":
            catalog, manifest, catalog_bytes = build_generation(args.spec)
            if manifest.generation_id != args.generation_id:
                raise MatrixportGenerationError(
                    "rebuilt generation differs from accepted candidate"
                )
            pointer = publish_generation(args.output_root, args.generation_id)
            result = {
                "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
                "generation_id": pointer.generation_id,
                "published": True,
                "state": "PUBLISHED",
            }
        else:
            raise SystemExit(f"unknown command: {args.command}")
    except (
        OSError,
        MatrixportGenerationError,
        PublicationRejectedError,
        ValueError,
    ) as exc:
        print(f"matrixport-generation: FAIL: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    if (
        args.command == "build"
        and result.get("coverage_status") != "COMPLETE_WITHIN_DECLARED_BOUNDARY"
    ):
        print(
            "matrixport-generation: BUILD FAILED — coverage is "
            f"{result['coverage_status']} (see undeclared_gaps / stale_declarations above)",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
