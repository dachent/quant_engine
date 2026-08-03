"""Run an isolated, read-only-to-source recovery rehearsal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from quant_engine.core import load_pipeline_spec
from quant_engine.core.rehearsal import RehearsalError, run_recovery_rehearsal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_recovery_rehearsal(
            spec=load_pipeline_spec(args.spec),
            release_id=args.release_id,
            output_root=args.output_root,
        )
    except (OSError, RehearsalError, RuntimeError, ValueError) as exc:
        print(f"data-recovery-rehearsal: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
