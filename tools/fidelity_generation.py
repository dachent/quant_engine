"""Build or stage one hash-bound, finite Fidelity activity generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from quant_engine.fidelity import build_generation, stage_generation_candidate


def _summary(spec_path: Path) -> dict[str, object]:
    catalog, manifest, catalog_bytes = build_generation(spec_path)
    return {
        "account_count": len(catalog["accounts"]),  # type: ignore[arg-type]
        "catalog_size_bytes": len(catalog_bytes),
        "cash_flow_count": len(catalog["cash_flows"]),  # type: ignore[arg-type]
        "corporate_action_count": len(catalog["corporate_actions"]),  # type: ignore[arg-type]
        "coverage_status": manifest.coverage.status.value,
        "generation_id": manifest.generation_id,
        "transaction_count": len(catalog["transactions"]),  # type: ignore[arg-type]
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="verify and build without writes")
    build.add_argument("spec", type=Path)
    stage = subparsers.add_parser("stage", help="create an immutable candidate")
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
