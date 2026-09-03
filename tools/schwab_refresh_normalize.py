"""Normalize one Schwab refresh envelope capture and emit a draft spec."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from quant_engine.schwab.refresh_normalize import (
    RefreshNormalizeError,
    normalize,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--prior-spec", type=Path, required=True)
    parser.add_argument("--prior-run-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--spec-out", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = normalize(
            run_root=args.run_root,
            prior_spec_path=args.prior_spec,
            prior_run_root=args.prior_run_root,
            output_dir=args.output_dir,
            spec_out=args.spec_out,
        )
    except (RefreshNormalizeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Never print account identifiers or crosswalk content here.
    print(
        json.dumps(
            {
                "account_count": result.account_count,
                "as_of_date": result.as_of_date,
                "gap_report_path": str(result.gap_report_path),
                "nav_count": result.nav_count,
                "position_count": result.position_count,
                "spec_path": str(result.spec_path),
                "transaction_count": result.transaction_count,
                "transaction_file_count": result.transaction_file_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
