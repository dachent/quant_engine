"""Value-safe command line access to offline web-contract extraction."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys
from typing import NoReturn

from quant_engine.web_contracts._version import VERSION
from quant_engine.web_contracts.evidence import HarParseStatus, extract_har


class _UsageError(ValueError):
    """An argument error whose user-supplied text must not be echoed."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _UsageError


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="quant-engine-web-contract",
        description="Preserve and materialize an existing HAR artifact offline.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    extract = subcommands.add_parser(
        "extract", help="Write a byte-complete, create-only extraction directory."
    )
    extract.add_argument(
        "--har-file",
        "--input",
        dest="source",
        type=Path,
        required=True,
        help="Existing HAR artifact to preserve and materialize.",
    )
    extract.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New create-only extraction directory.",
    )
    return parser


def _json_line(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _error(code: str, exit_code: int = 1) -> int:
    print(_json_line({"error": code, "ok": False}), file=sys.stderr)
    return exit_code


def _run(argv: Sequence[str] | None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _UsageError:
        return _error("CLI_USAGE", 64)
    try:
        receipt = extract_har(args.source, args.output_dir)
    except FileNotFoundError:
        code = "IO_ERROR" if args.source.exists() else "SOURCE_READ_FAILED"
        return _error(code)
    except FileExistsError:
        return _error("OUTPUT_EXISTS")
    except OSError:
        return _error("IO_ERROR")
    except Exception:
        return _error("INTERNAL_ERROR")
    print(
        _json_line(
            {
                "materialized_entry_count": receipt.materialized_entry_count,
                "ok": receipt.parse_status is HarParseStatus.COMPLETE,
                "parse_status": receipt.parse_status,
                "source_sha256": receipt.source_sha256,
                "source_size_bytes": receipt.source_size_bytes,
            }
        )
    )
    return 0 if receipt.parse_status is HarParseStatus.COMPLETE else 2


def main(argv: Sequence[str] | None = None) -> int:
    """Run the generalized web-contract command group."""

    try:
        return _run(argv)
    except KeyboardInterrupt:
        return _error("INTERRUPTED", 130)
