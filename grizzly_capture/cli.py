"""argv-tuple entry point for the supervised GrizzlyBulls capture run.

Usage:
    python -m grizzly_capture.cli --run-dir <dir> --cdp-port 9223 \
        --model-slug <slug> [--route {auto,download,state}]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from grizzly_capture import (
    CredentialScanError,
    LoginRequiredError,
    RouteFailure,
    RunDirExistsError,
    SessionError,
)
from grizzly_capture.manifest import (
    STATUS_NON_TERMINAL,
    STATUS_TERMINAL,
    CaptureManifest,
)
from grizzly_capture.pagination import capture_positions, run_route
from grizzly_capture.persistence import (
    SESSION_EVIDENCE_FILENAME,
    ArtifactRecord,
    create_run_dir,
    sha256_bytes,
    write_artifact,
)
from grizzly_capture.session import (
    DEFAULT_BASE_URL,
    DEFAULT_CDP_PORT,
    browser_session,
    first_or_new_page,
    navigate_and_idle,
    require_authenticated,
    sanitized_page_url,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOGIN_REQUIRED = 2
EXIT_ROUTES_FAILED = 3
EXIT_RUN_DIR_EXISTS = 4

ROUTE_ORDER = ("download", "state")

STATE_DERIVED_NOTE = (
    "trades artifact is a state-derived serialization of the page's React "
    "state, not the provider's own CSV bytes"
)


def model_url(base_url: str, model_slug: str) -> str:
    return f"{base_url.rstrip('/')}/models/{model_slug}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grizzly_capture",
        description=(
            "Supervised authenticated capture of GrizzlyBulls model surfaces "
            "(trades + current positions) via a CDP-attached browser."
        ),
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="create-only output directory for this run",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=DEFAULT_CDP_PORT,
        help="remote-debugging port of the owner-launched browser",
    )
    parser.add_argument(
        "--model-slug",
        required=True,
        help="model page slug, e.g. vix-ta-macro-advanced",
    )
    parser.add_argument(
        "--route",
        choices=("auto", "download", "state"),
        default="auto",
        help="trades route override; 'auto' tries download then state",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="site origin for the model pages",
    )
    parser.add_argument(
        "--download-timeout",
        type=float,
        default=120.0,
        help="seconds to wait for the blob download to land",
    )
    return parser


def write_session_evidence(page: Any, run_dir: Path) -> ArtifactRecord:
    """Persist minimal session evidence; the page title is stored hashed."""
    title = str(page.title())
    payload = {
        "page_url": sanitized_page_url(page),
        "page_title_sha256": sha256_bytes(title.encode("utf-8")),
        "authenticated": True,
        "captured_utc": datetime.now(timezone.utc).isoformat(),
    }
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    return write_artifact(
        run_dir,
        SESSION_EVIDENCE_FILENAME,
        data + b"\n",
        source="session-evidence",
    )


def run_capture(
    page: Any,
    *,
    run_dir: Path,
    model_slug: str,
    cdp_port: int = DEFAULT_CDP_PORT,
    base_url: str = DEFAULT_BASE_URL,
    route: str = "auto",
    download_timeout_s: float = 120.0,
) -> int:
    """Drive one capture run over an attached page. Returns an exit code.

    Fail-closed: when the authenticated marker is absent this raises
    ``LoginRequiredError`` before anything is written.
    """
    navigate_and_idle(page, model_url(base_url, model_slug))
    require_authenticated(page)
    create_run_dir(run_dir)
    manifest = CaptureManifest(
        model_slug=model_slug,
        base_url=base_url,
        cdp_port=cdp_port,
        route_requested=route,
    )
    routes = list(ROUTE_ORDER) if route == "auto" else [route]
    artifact = None
    for candidate in routes:
        try:
            artifact = run_route(
                candidate,
                page,
                run_dir,
                timeout_s=download_timeout_s,
            )
            manifest.route_used = candidate
            manifest.attempts.append({"route": candidate, "outcome": "success"})
            break
        except RouteFailure as exc:
            manifest.attempts.append(
                {"route": candidate, "outcome": "failed", "detail": str(exc)},
            )
    if artifact is None:
        manifest.artifacts.append(write_session_evidence(page, run_dir))
        manifest.notes.append(
            "all trades routes failed; session evidence only, no capture artifacts",
        )
        manifest.status = STATUS_NON_TERMINAL
        manifest.finalize(run_dir)
        return EXIT_ROUTES_FAILED
    manifest.artifacts.append(artifact)
    if manifest.route_used == "state":
        manifest.notes.append(STATE_DERIVED_NOTE)
    positions = capture_positions(page, run_dir)
    if positions is None:
        manifest.notes.append("positions surface not found on the driven page")
    else:
        manifest.artifacts.append(positions)
    manifest.status = STATUS_TERMINAL
    manifest.finalize(run_dir)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with browser_session(args.cdp_port) as session:
            page = first_or_new_page(session, base_url=args.base_url)
            return run_capture(
                page,
                run_dir=Path(args.run_dir),
                model_slug=args.model_slug,
                cdp_port=args.cdp_port,
                base_url=args.base_url,
                route=args.route,
                download_timeout_s=args.download_timeout,
            )
    except LoginRequiredError:
        print(
            "owner login required: log in inside the driven browser window, "
            "then rerun the command",
            file=sys.stderr,
        )
        return EXIT_LOGIN_REQUIRED
    except RunDirExistsError:
        print(
            "run directory already exists; run directories are create-only",
            file=sys.stderr,
        )
        return EXIT_RUN_DIR_EXISTS
    except CredentialScanError as exc:
        print(f"credential scan failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SessionError as exc:
        print(f"session error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
