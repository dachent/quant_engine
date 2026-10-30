"""Command line for the IBKR Flex capture driver.

``python -m quant_engine.ibkr.capture <command>``.  Offline commands
(``plan-hash``, ``normalize``, ``verify``, ``symbols``) never touch the network
or credentials.  ``live`` refuses unless reviewed governance evidence for the
exact plan is supplied, and it is never run by the repository's tests against
a provider.  Output is aggregate-only: counts, hashes and stable reason codes.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import tomllib
from typing import Any

from .credentials import CredentialError, resolve_flex_credentials
from quant_engine.ibkr.flex import HttpGet

from ._util import canonical_json_bytes
from .catalog import catalog_sha256
from .governance import GovernanceError, authorize_live_read
from .normalize import NormalizationError, normalize_run
from .plan import PlanError, load_plan
from .securities import SecurityScanError, SymbolTable
from .session import CaptureError, RunSummary, capture_plan
from .staging import RawStage, StagingError, write_new_file
from .transport import TransportError, UrllibHttpGet
from .verify import VerificationError, verify_run

EXIT_OK = 0
EXIT_REFUSED = 2


def run_live(
    *,
    plan_path: Path,
    governance_path: Path,
    config_path: Path,
    receipt_root: Path,
    staging_root: Path,
    user_agent: str,
    http_get: HttpGet | None = None,
    wait: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
    sources: Mapping[str, Any] | None = None,
) -> list[RunSummary]:
    """The governed live path: governance first, credentials second, then I/O."""

    plan = load_plan(plan_path.read_bytes())
    stage = RawStage(staging_root)
    governance = authorize_live_read(
        governance_path, plan=plan, receipt_root=receipt_root, now=now
    )
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    credentials = resolve_flex_credentials(config, plan, sources=sources)
    return capture_plan(
        plan,
        governance=governance,
        credentials=credentials,
        http_get=http_get if http_get is not None else UrllibHttpGet(),
        wait=wait,
        stage=stage,
        user_agent=user_agent,
        now=now,
    )


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant_engine.ibkr.capture", description=__doc__
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan_hash = commands.add_parser("plan-hash", help="validate a request plan offline")
    plan_hash.add_argument("plan", type=Path)
    normalize = commands.add_parser("normalize", help="normalize one staged run")
    normalize.add_argument("run_dir", type=Path)
    normalize.add_argument("output", type=Path)
    verify = commands.add_parser("verify", help="independently verify one run")
    verify.add_argument("run_dir", type=Path)
    verify.add_argument("normalized", type=Path)
    symbols = commands.add_parser("symbols", help="build the conid symbol table")
    symbols.add_argument("--normalized", type=Path, action="append", required=True)
    symbols.add_argument(
        "--required", type=Path, help="file of security keys, one per line"
    )
    symbols.add_argument("--output", type=Path, required=True)
    live = commands.add_parser("live", help="governed live capture (never in CI)")
    live.add_argument("--plan", type=Path, required=True)
    live.add_argument("--governance", type=Path, required=True)
    live.add_argument("--config", type=Path, required=True)
    live.add_argument("--receipt-root", type=Path, required=True)
    live.add_argument("--staging-root", type=Path, required=True)
    live.add_argument("--user-agent", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan-hash":
            plan = load_plan(args.plan.read_bytes())
            _emit(
                {
                    "catalog_sha256": catalog_sha256(),
                    "plan_sha256": plan.sha256,
                    "requests": len(plan.requests),
                }
            )
        elif args.command == "normalize":
            document = normalize_run(args.run_dir, args.output)
            _emit(
                {
                    "coverage": document["coverage"]["status"],
                    "securities": len(document["securities"]),
                    "statements": document["coverage"]["statement_count"],
                }
            )
        elif args.command == "verify":
            report = verify_run(args.run_dir, args.normalized)
            _emit(
                {
                    "checks": list(report.checks),
                    "coverage": report.coverage_status,
                    "securities": report.security_count,
                    "statements": report.statement_count,
                    "status": report.status,
                }
            )
        elif args.command == "symbols":
            rows: list[Mapping[str, Any]] = []
            for path in args.normalized:
                document = json.loads(path.read_text(encoding="utf-8"))
                rows.extend(document["securities"])
            table = SymbolTable.from_rows(rows)
            write_new_file(args.output, canonical_json_bytes(table.to_document()))
            summary: dict[str, Any] = {"entries": len(table.to_document()["entries"])}
            if args.required is not None:
                keys = [
                    line.strip()
                    for line in args.required.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                summary["report"] = table.report(keys)
            _emit(summary)
        else:
            summaries = run_live(
                plan_path=args.plan,
                governance_path=args.governance,
                config_path=args.config,
                receipt_root=args.receipt_root,
                staging_root=args.staging_root,
                user_agent=args.user_agent,
            )
            _emit(
                {
                    "runs": [
                        {
                            "attempts": item.attempts,
                            "refusal_code": item.refusal_code,
                            "refusal_kind": item.refusal_kind,
                            "request_index": item.request_index,
                            "run_id": item.run_id,
                            "status": item.status,
                        }
                        for item in summaries
                    ]
                }
            )
            if any(item.status != "STAGED" for item in summaries):
                return EXIT_REFUSED
    except (
        CaptureError,
        CredentialError,
        GovernanceError,
        NormalizationError,
        PlanError,
        SecurityScanError,
        StagingError,
        TransportError,
        VerificationError,
        OSError,
        KeyError,
        ValueError,
    ) as exc:
        payload: dict[str, str] = {"error": type(exc).__name__}
        if isinstance(
            exc,
            (
                CaptureError,
                GovernanceError,
                NormalizationError,
                PlanError,
                SecurityScanError,
                StagingError,
                VerificationError,
            ),
        ):
            # These classes carry fixed, value-free messages written for output.
            payload["reason"] = str(exc)
        _emit(payload)
        return EXIT_REFUSED
    return EXIT_OK
