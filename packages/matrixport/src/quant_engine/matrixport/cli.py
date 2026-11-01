from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .credentials import CredentialError, load_credentials
from .evidence import (
    DEFAULT_MAX_WINDOW_HOURS,
    EvidenceGenerationError,
    EvidenceRequest,
    verify_evidence,
    write_evidence,
)
from .pipeline import (
    DATABASE_NAME,
    MatrixportExtractor,
    export_catalog,
    export_run,
    live_auth_test,
    _credential_identity,
    recorded_credential_identity,
    validate_run_id,
)
from .storage import RunStorage
from .util import atomic_write, canonical_json
from .verification import verify_run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-engine-matrixport",
        description="Read-only Matrixport/BIT data extraction and export",
    )
    parser.add_argument(
        "--credentials-config",
        type=Path,
        help=(
            "TOML file with a [credentials.matrixport] plan naming where the "
            "keys are read from (Azure Key Vault, Bitwarden, Windows Credential "
            "Manager); the file holds references, never values. Without it the "
            "Windows generic credential BIT.com-API is used."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    auth_test = subcommands.add_parser(
        "auth-test", help="Test BIT.com-API using one Wallet read"
    )
    auth_test.add_argument("--governance-evidence", required=True)
    subcommands.add_parser("catalog", help="Write the durable endpoint catalog")

    extract = subcommands.add_parser("extract", help="Extract raw and canonical data")
    mode = extract.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--full", action="store_true", help="Fetch all API-reachable history"
    )
    mode.add_argument(
        "--incremental", action="store_true", help="Fetch overlapping changes"
    )
    extract.add_argument(
        "--resume", metavar="RUN_ID", help="Resume an interrupted RUNNING run"
    )
    extract.add_argument("--governance-evidence", required=True)

    export = subcommands.add_parser("export", help="Generate CSV and XLSX outputs")
    export.add_argument("--run-id", required=True)

    verify = subcommands.add_parser(
        "verify", help="Verify raw, state, CSV, XLSX, and privacy"
    )
    verify.add_argument("--run-id", required=True)

    gen = subcommands.add_parser(
        "governance-evidence",
        help="Assemble (never approve) a single-use live-read evidence file",
    )
    gen.add_argument(
        "--operation", required=True, choices=("auth-test", "extract", "run")
    )
    gen.add_argument("--mode", choices=("full", "incremental"))
    gen.add_argument("--cutoff", required=True, help="YYYY-MM-DD")
    gen.add_argument("--review-url", required=True)
    gen.add_argument("--review-identity", required=True)
    gen.add_argument("--issued-at", required=True)
    gen.add_argument("--expires-at", required=True)
    gen.add_argument("--max-requests", required=True, type=int)
    gen.add_argument("--max-window-hours", type=float, default=DEFAULT_MAX_WINDOW_HOURS)
    gen.add_argument(
        "--scope-path",
        action="append",
        help="assert the scope: repeat for every GET path; must equal the catalog reads",
    )
    gen.add_argument("--out", required=True, type=Path)

    check = subcommands.add_parser(
        "verify-evidence",
        help="Validate an evidence file with the live gate's validator, unconsumed",
    )
    check.add_argument("evidence", type=Path)
    check.add_argument(
        "--max-window-hours", type=float, default=DEFAULT_MAX_WINDOW_HOURS
    )

    run = subcommands.add_parser("run", help="Extract, export, and verify")
    run.add_argument("--full", action="store_true", required=True)
    run.add_argument("--governance-evidence", required=True)
    return parser


def _workspace() -> Path:
    return Path.cwd().resolve()


def _report(message: str) -> None:
    print(f"[matrixport] {message}", file=sys.stderr, flush=True)


def _require_recorded_credentials(
    run_dir: Path, run_id: str, config_path: Path | None
) -> None:
    """Scan for the credentials that produced the run, not whichever are current."""

    storage = RunStorage(run_dir / "state" / DATABASE_NAME)
    try:
        run = storage.get_run(run_id)
    finally:
        storage.close()
    if run is not None and recorded_credential_identity(run) != _credential_identity(
        None, config_path
    ):
        raise CredentialError(
            "The credential source does not match the one recorded for this run"
        )


def _secret_needles(config_path: Path | None = None) -> list[bytes]:
    credentials = load_credentials(config_path)
    values = (credentials.key_identifier, credentials.signing_material)
    needles: list[bytes] = []
    for value in values:
        needles.extend((value.encode("utf-8"), value.encode("utf-16le")))
    return needles


def _extract(args: argparse.Namespace) -> int:
    mode = "incremental" if args.incremental else "full"
    extractor = MatrixportExtractor(
        _workspace(),
        mode=mode,
        run_id=args.resume,
        resume=bool(args.resume),
        credentials_config=args.credentials_config,
        governance_evidence=args.governance_evidence,
        reporter=_report,
    )
    try:
        result = extractor.extract()
    finally:
        extractor.close()
    print(
        canonical_json(
            {
                "run_id": result.run_id,
                "status": result.status,
                "request_count": result.request_count,
                "dataset_count": len(result.dataset_counts),
                "run_dir": str(result.run_dir),
            }
        )
    )
    return 0


def _verify(run_id: str, config_path: Path | None = None) -> tuple[int, dict[str, Any]]:
    validate_run_id(run_id)
    run_dir = _workspace() / "output" / run_id
    _require_recorded_credentials(run_dir, run_id, config_path)
    result = verify_run(run_dir, _secret_needles(config_path))
    release_path = run_dir / "reports" / "release_status.json"
    try:
        release = json.loads(release_path.read_text(encoding="utf-8"))
    except Exception:
        release = {}
    release.update(
        {
            "status": result["status"] if result["ok"] else "FAILED",
            "verification_ok": result["ok"],
        }
    )
    atomic_write(
        release_path,
        (canonical_json(release) + "\n").encode("utf-8"),
    )
    print(canonical_json(result))
    return (0 if result["ok"] else 2), result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "auth-test":
            print(
                canonical_json(
                    live_auth_test(
                        _workspace(),
                        args.governance_evidence,
                        credentials_config=args.credentials_config,
                    )
                )
            )
            return 0
        if args.command == "governance-evidence":
            print(
                canonical_json(
                    write_evidence(
                        EvidenceRequest(
                            operation=args.operation,
                            mode=args.mode,
                            cutoff=args.cutoff,
                            review_url=args.review_url,
                            review_identity=args.review_identity,
                            issued_at=args.issued_at,
                            expires_at=args.expires_at,
                            max_requests=args.max_requests,
                            max_window_hours=args.max_window_hours,
                            scope_paths=(
                                tuple(args.scope_path) if args.scope_path else None
                            ),
                            credentials_config=args.credentials_config,
                        ),
                        args.out,
                    )
                )
            )
            return 0
        if args.command == "verify-evidence":
            print(
                canonical_json(
                    verify_evidence(
                        args.evidence, max_window_hours=args.max_window_hours
                    )
                )
            )
            return 0
        if args.command == "catalog":
            csv_path, json_path = export_catalog(_workspace())
            print(
                canonical_json(
                    {"ok": True, "csv": str(csv_path), "json": str(json_path)}
                )
            )
            return 0
        if args.command == "extract":
            return _extract(args)
        if args.command == "export":
            result = export_run(_workspace(), args.run_id)
            print(
                canonical_json(
                    {
                        "ok": True,
                        "run_id": args.run_id,
                        "csv_count": len(result["csv"]),
                        "workbooks": {
                            key: str(value) for key, value in result["xlsx"].items()
                        },
                    }
                )
            )
            return 0
        if args.command == "verify":
            return _verify(args.run_id, args.credentials_config)[0]
        if args.command == "run":
            credentials = load_credentials(args.credentials_config)
            extractor = MatrixportExtractor(
                _workspace(),
                mode="full",
                credentials=credentials,
                credentials_config=args.credentials_config,
                governance_evidence=args.governance_evidence,
                live_operation="run",
                reporter=_report,
            )
            try:
                extraction = extractor.extract()
            finally:
                extractor.close()
            exported = export_run(_workspace(), extraction.run_id)
            result = verify_run(
                extraction.run_dir,
                [
                    credentials.key_identifier.encode("utf-8"),
                    credentials.key_identifier.encode("utf-16le"),
                    credentials.signing_material.encode("utf-8"),
                    credentials.signing_material.encode("utf-16le"),
                ],
            )
            atomic_write(
                extraction.run_dir / "reports" / "release_status.json",
                (
                    canonical_json(
                        {
                            "status": result["status"] if result["ok"] else "FAILED",
                            "extraction_status": extraction.status,
                            "export_status": "COMPLETED",
                            "verification_ok": result["ok"],
                        }
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            print(
                canonical_json(
                    {
                        "ok": result["ok"],
                        "run_id": extraction.run_id,
                        "request_count": extraction.request_count,
                        "csv_count": len(exported["csv"]),
                        "workbook_count": len(exported["xlsx"]),
                    }
                )
            )
            return 0 if result["ok"] else 2
    except EvidenceGenerationError as error:
        print(
            canonical_json(
                {"ok": False, "error": "EvidenceRefused", "message": str(error)}
            ),
            file=sys.stderr,
        )
        return 4
    except CredentialError as error:
        print(
            canonical_json(
                {"ok": False, "error": type(error).__name__, "message": str(error)}
            ),
            file=sys.stderr,
        )
        return 3
    except Exception as error:
        print(
            canonical_json(
                {"ok": False, "error": type(error).__name__, "message": str(error)}
            ),
            file=sys.stderr,
        )
        return 1
    return 1
