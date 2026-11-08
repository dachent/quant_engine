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
    cutoff_end_ms,
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
    MAX_UNLABELLED_GAP_HOURS,
    SNAPSHOT_PATHS,
    inherited_snapshot_captures,
    late_capture_exceeds_bound,
    late_capture_fact,
    recorded_capture_label,
    recorded_captures,
    recorded_cutoff,
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
    extract.add_argument(
        "--cutoff",
        required=True,
        help="YYYY-MM-DD; must equal the cutoff in the evidence provenance sidecar",
    )

    export = subcommands.add_parser("export", help="Generate CSV and XLSX outputs")
    export.add_argument("--run-id", required=True)

    verify = subcommands.add_parser(
        "verify", help="Verify raw, state, CSV, XLSX, and privacy"
    )
    verify.add_argument("--run-id", required=True)
    verify.add_argument(
        "--cutoff", help="refuse unless the run was bound to this cutoff"
    )

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
    gen.add_argument(
        "--captured-after-cutoff-label",
        help=(
            "explicit reason the current-state requests may be captured after the "
            "cutoff day; without it verify refuses such a run"
        ),
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
    run.add_argument(
        "--cutoff",
        required=True,
        help="YYYY-MM-DD; must equal the cutoff in the evidence provenance sidecar",
    )
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
        cutoff=args.cutoff,
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


def _require_recorded_cutoff(
    run_dir: Path, run_id: str, cutoff: str | None
) -> dict[str, Any] | None:
    """A cutoff-bound run must match the cutoff asked for and end inside it.

    Returns the late-capture fact (captured times after the cutoff day's end, with the gap in
    hours) when there is one: it is recorded, not approved.  A gap above
    ``MAX_UNLABELLED_GAP_HOURS`` or a capture time that was never recorded still needs the
    ``captured_after_cutoff_label``.
    """

    storage = RunStorage(run_dir / "state" / DATABASE_NAME)
    try:
        run = storage.get_run(run_id)
    finally:
        storage.close()
    if run is None:
        return None
    recorded = recorded_cutoff(run)
    if cutoff is not None and recorded != cutoff:
        raise CredentialError("The run was not bound to the requested cutoff")
    if recorded is not None:
        try:
            limit = cutoff_end_ms(recorded, 2**62)
        except EvidenceGenerationError:
            raise CredentialError("The recorded cutoff is invalid") from None
        if int(run["run_end_ms"]) > limit:
            raise CredentialError("The run window extends past its cutoff")
        captures = recorded_captures(run)
        missing = [path for path in SNAPSHOT_PATHS if path not in captures]
        if missing and run["status"] == "COMPLETED":
            raise CredentialError("The run did not record its capture times")
        label = recorded_capture_label(run)
        direct_late = [at_ms for at_ms in captures.values() if at_ms > limit]
        if (
            direct_late
            and late_capture_exceeds_bound(max(direct_late), limit)
            and label is None
        ):
            gap = round((max(direct_late) - limit) / 3_600_000, 4)
            raise CredentialError(
                f"Current-state requests were captured {gap} h after the "
                f"cutoff day (more than {MAX_UNLABELLED_GAP_HOURS} h) and the evidence "
                "carries no captured_after_cutoff_label"
            )
        inherited = inherited_snapshot_captures(run_dir, run_id)
        if any(at_ms is None for _dataset, _base, at_ms in inherited):
            if label is None:
                raise CredentialError(
                    "Inherited current-state rows were captured without a recorded "
                    "capture time and the evidence carries no captured_after_cutoff_label"
                )
        inherited_times = {
            f"{dataset}:{base_id}": at_ms
            for dataset, base_id, at_ms in inherited
            if at_ms is not None
        }
        inherited_late = [at_ms for at_ms in inherited_times.values() if at_ms > limit]
        if (
            inherited_late
            and late_capture_exceeds_bound(max(inherited_late), limit)
            and label is None
        ):
            inherited_gap = round((max(inherited_late) - limit) / 3_600_000, 4)
            raise CredentialError(
                "Inherited current-state rows were captured "
                f"{inherited_gap} h after the cutoff day (more than "
                f"{MAX_UNLABELLED_GAP_HOURS} h) and the evidence carries no "
                "captured_after_cutoff_label"
            )
        return late_capture_fact(captures, limit, inherited_times)
    return None


def _verify(
    run_id: str,
    config_path: Path | None = None,
    cutoff: str | None = None,
    *,
    emit: bool = True,
    secret_needles: list[bytes] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Verify a run; the standalone ``verify`` and the combined ``run`` both use this.

    ``secret_needles`` are the exact credentials that signed the run's requests (the
    combined ``run`` command has them and must not read the credential source again).
    """

    validate_run_id(run_id)
    run_dir = _workspace() / "output" / run_id
    if secret_needles is None:
        _require_recorded_credentials(run_dir, run_id, config_path)
    late_capture = _require_recorded_cutoff(run_dir, run_id, cutoff)
    result = verify_run(
        run_dir,
        _secret_needles(config_path) if secret_needles is None else secret_needles,
    )
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
    if late_capture is not None:
        release["late_capture"] = late_capture
        result["late_capture"] = late_capture
    atomic_write(
        release_path,
        (canonical_json(release) + "\n").encode("utf-8"),
    )
    if emit:
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
                            captured_after_cutoff_label=args.captured_after_cutoff_label,
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
            return _verify(args.run_id, args.credentials_config, args.cutoff)[0]
        if args.command == "run":
            credentials = load_credentials(args.credentials_config)
            extractor = MatrixportExtractor(
                _workspace(),
                mode="full",
                credentials=credentials,
                credentials_config=args.credentials_config,
                governance_evidence=args.governance_evidence,
                live_operation="run",
                cutoff=args.cutoff,
                reporter=_report,
            )
            try:
                extraction = extractor.extract()
            finally:
                extractor.close()
            exported = export_run(_workspace(), extraction.run_id)
            atomic_write(
                extraction.run_dir / "reports" / "release_status.json",
                (
                    canonical_json(
                        {
                            "extraction_status": extraction.status,
                            "export_status": "COMPLETED",
                        }
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            _code, result = _verify(
                extraction.run_id,
                args.credentials_config,
                args.cutoff,
                emit=False,
                secret_needles=[
                    credentials.key_identifier.encode("utf-8"),
                    credentials.key_identifier.encode("utf-16le"),
                    credentials.signing_material.encode("utf-8"),
                    credentials.signing_material.encode("utf-16le"),
                ],
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
