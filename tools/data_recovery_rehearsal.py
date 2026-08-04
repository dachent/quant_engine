"""Run an isolated, read-only-to-source recovery rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
from typing import Sequence

from quant_engine.core import load_pipeline_spec
import quant_engine.core.rehearsal as rehearsal_module
from quant_engine.core.rehearsal import RehearsalError, run_recovery_rehearsal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--harness-code-commit", required=True)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    module_path = Path(str(rehearsal_module.__file__)).resolve()
    entrypoint_path = Path(__file__).resolve()
    executable_path = Path(sys.executable).resolve()
    try:
        result = run_recovery_rehearsal(
            spec=load_pipeline_spec(args.spec),
            release_id=args.release_id,
            output_root=args.output_root,
            harness_identity={
                "code_commit": args.harness_code_commit,
                "core_module_sha256": _sha256(module_path),
                "entrypoint_sha256": _sha256(entrypoint_path),
                "python_executable_sha256": _sha256(executable_path),
                "python_version": platform.python_version(),
                "runtime_platform": platform.platform(),
            },
        )
    except (OSError, RehearsalError, RuntimeError, ValueError) as exc:
        print(f"data-recovery-rehearsal: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
