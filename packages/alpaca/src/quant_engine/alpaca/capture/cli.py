"""Command line for the Alpaca capture driver: ``plan``, ``run``, ``verify``, ``ingest``.

``plan`` and ``verify`` never touch the network or a credential.  ``run``
refuses unless governance evidence is supplied, consumes it before any secret is
resolved, and prints aggregate counts and hashes only (restricted evidence
rules): never an account identifier, a balance, or a row.  This change never
executes ``run`` against a live provider.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import tomllib
from typing import Any

from quant_engine.alpaca.capture.catalog import catalog_sha256
from quant_engine.alpaca.capture.config import (
    CaptureConfig,
    capture_config_from_mapping,
    credential_plan,
)
from quant_engine.alpaca.capture.errors import (
    CaptureError,
    CaptureIncomplete,
    CaptureRefused,
)
from quant_engine.alpaca.capture.fetch import GovernedFetcher, HttpsFetcher, RawFetcher
from quant_engine.alpaca.capture.governance import TERMS_SHA256, authorize_live_read
from quant_engine.alpaca.capture.ingest import ingest_run, load_crosswalk
from quant_engine.alpaca.capture.signcheck import run_sign_check, write_crosswalk
from quant_engine.alpaca.capture.run import run_capture
from quant_engine.alpaca.capture.util import sha256_text
from quant_engine.alpaca.capture.verify import verify_run
from quant_engine.core import (
    CredentialError,
    CredentialResolver,
    SecretValue,
    sources_from_config,
)

FetcherFactory = Callable[[CaptureConfig, Mapping[str, SecretValue]], RawFetcher]


def _load(path: str) -> dict[str, Any]:
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise CaptureRefused("the configuration file cannot be read as TOML") from None


def _default_factory(
    capture: CaptureConfig, values: Mapping[str, SecretValue]
) -> RawFetcher:
    return HttpsFetcher(capture.environment, values["key_id"], values["key_secret"])


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m quant_engine.alpaca.capture")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="show the exact planned requests; no I/O")
    plan.add_argument("--config", required=True)
    run = commands.add_parser("run", help="governed live capture")
    run.add_argument("--config", required=True)
    run.add_argument("--governance-evidence", required=True)
    run.add_argument("--receipt-root", required=True)
    run.add_argument("--staging-root", required=True)
    run.add_argument("--run-id")
    verify = commands.add_parser("verify", help="independently verify a staged run")
    verify.add_argument("--run-dir", required=True)
    ingest = commands.add_parser(
        "ingest",
        help="turn a verified run into the native activities package; no network",
    )
    ingest.add_argument("--run-dir", required=True)
    ingest.add_argument("--crosswalk", required=True)
    ingest.add_argument("--internal-account-id", required=True)
    ingest.add_argument("--after", required=True)
    ingest.add_argument("--until", required=True)
    ingest.add_argument("--output-root", required=True)
    ingest.add_argument("--package-id")
    cross = commands.add_parser(
        "crosswalk",
        help="write the one-row crosswalk the ingest consumes; no network",
    )
    cross.add_argument("--plan-digest", required=True)
    cross.add_argument("--internal-account-id", required=True)
    cross.add_argument("--out", required=True)
    sign = commands.add_parser(
        "signcheck",
        help="check reported cash signs against the balance movement; no network",
    )
    sign.add_argument("--package-dir", required=True)
    sign.add_argument("--opening-cash", required=True)
    sign.add_argument("--closing-cash")
    sign.add_argument("--closing-from-snapshot", action="store_true")
    sign.add_argument("--tolerance", default="0")
    sign.add_argument("--out")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher_factory: FetcherFactory | None = None,
    sources: Mapping[str, Any] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            report = verify_run(args.run_dir)
            _emit(
                {
                    "ok": report.ok,
                    "findings": list(report.findings),
                    "request_count": report.request_count,
                    "activity_count": report.activity_count,
                }
            )
            return 0 if report.ok else 1
        if args.command == "crosswalk":
            digest = write_crosswalk(
                plan_digest=args.plan_digest,
                internal_account_id=args.internal_account_id,
                out=args.out,
            )
            _emit({"crosswalk_sha256": digest})
            return 0
        if args.command == "signcheck":
            checked = run_sign_check(
                package_directory=args.package_dir,
                opening_cash=args.opening_cash,
                closing_cash=args.closing_cash,
                closing_from_snapshot=args.closing_from_snapshot,
                tolerance=args.tolerance,
                out=args.out,
            )
            _emit(checked.to_dict())
            return 0 if checked.blessed else 1
        if args.command == "ingest":
            ingested = ingest_run(
                run_directory=args.run_dir,
                crosswalk=load_crosswalk(args.crosswalk),
                internal_account_id=args.internal_account_id,
                expect_after=args.after,
                expect_until=args.until,
                output_root=args.output_root,
                package_id=args.package_id,
            )
            _emit(
                {
                    "package": ingested.package_directory.name,
                    "binding_sha256": ingested.binding_sha256,
                    "activity_count": ingested.activity_count,
                }
            )
            return 0
        config = _load(args.config)
        capture = capture_config_from_mapping(config)
        plan = credential_plan(config)
        if args.command == "plan":
            _emit(
                {
                    "environment": capture.environment,
                    "base_url": capture.base_url,
                    "planned_endpoints": list(capture.planned_endpoints()),
                    "account_binding_sha256": sha256_text(capture.account_id),
                    "catalog_sha256": catalog_sha256(),
                    "terms_sha256": TERMS_SHA256,
                    "credential_fields": sorted(plan.fields),
                }
            )
            return 0
        governance = authorize_live_read(
            args.governance_evidence,
            config=capture,
            receipt_root=args.receipt_root,
        )
        resolver = CredentialResolver(
            dict(sources) if sources is not None else sources_from_config(config)
        )
        values = resolver.resolve(plan)
        raw = (fetcher_factory or _default_factory)(capture, values)
        run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        result = run_capture(
            config=capture,
            fetcher=GovernedFetcher(raw, governance),
            governance=governance,
            staging_root=args.staging_root,
            run_id=run_id,
        )
        report = verify_run(result.run_directory)
        _emit(
            {
                "run_id": run_id,
                "request_count": result.request_count,
                "activity_count": result.activity_count,
                "manifest_sha256": result.manifest_sha256,
                "verified": report.ok,
                "findings": list(report.findings),
            }
        )
        return 0 if report.ok else 1
    except CaptureIncomplete as error:
        _emit({"error": str(error), "reason": error.reason})
        return 2
    except (CaptureError, CredentialError) as error:
        _emit({"error": str(error)})
        return 2
