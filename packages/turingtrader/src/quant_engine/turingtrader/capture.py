"""Capture and deterministic replay for TuringTrader public-token endpoints.

Tokens come only from a deployment-local config file (never committed)::

    {"version": 1,
     "tokens": {"public": "...", "allochist": "...", "holdings": "..."}}

Capture persists exact response bytes before any parsing, validates every
payload against the versioned schemas, normalizes deterministically, and fails
closed on schema drift, stale dated endpoints, or token leakage.  Replay
re-reads the raw bytes, verifies every hash, re-runs normalization, and exits
zero only if every persisted output is reproduced exactly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .config import TokenConfig, load_config
from .endpoints import (
    ENDPOINTS,
    Endpoint,
    build_url,
    get_endpoint,
    sanitized_url,
    validate_slug,
)
from .errors import (
    ConfigError,
    RawStoreError,
    TransportError,
    TuringTraderError,
)
from .freshness import FreshnessVerdict, evaluate
from .normalize import normalize
from .raw_store import (
    canonical_json,
    create_run_dir,
    sha256_bytes,
    write_create_only,
    write_metadata,
)
from .schemas import validate
from .transport import Fetch, urllib_fetch


EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_USAGE = 2

FAILING_VERDICTS = frozenset({"stale", "empty"})


def _report(message: str) -> None:
    print(f"[turingtrader] {message}", file=sys.stderr, flush=True)


def _parse_payload(endpoint: Endpoint, body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TuringTraderError(
            f"{endpoint.object_name} response is not valid UTF-8 JSON"
        ) from error


def _capture_endpoint(
    *,
    run_dir: Path,
    slug: str,
    endpoint: Endpoint,
    config: TokenConfig,
    run_date: date,
    grace_days: int,
    fetch: Fetch,
) -> FreshnessVerdict:
    """Fetch, persist raw bytes first, then validate and normalize."""
    url = build_url(slug, endpoint, config.token(endpoint.token_key))
    status_code, body = fetch(url)

    raw_rel = Path("raw") / slug / f"{endpoint.object_name}.bin"
    # Raw bytes are durable before any parsing or status handling.
    write_create_only(run_dir / raw_rel, body)

    if not 200 <= status_code < 300:
        raise TransportError(
            f"{endpoint.object_name} returned status {status_code} for slug {slug!r}"
        )
    payload = _parse_payload(endpoint, body)
    validate(endpoint, payload)
    rows = normalize(endpoint.object_name, payload)
    verdict = evaluate(
        endpoint.object_name,
        carries_dates=endpoint.carries_dates,
        rows=rows,
        run_date=run_date,
        grace_days=grace_days,
    )

    normalized_rel = Path("normalized") / slug / f"{endpoint.object_name}.json"
    write_create_only(
        run_dir / normalized_rel, (canonical_json(rows) + "\n").encode("utf-8")
    )

    envelope = {
        "schema_version": 1,
        "object": endpoint.object_name,
        "slug": slug,
        "token_key": endpoint.token_key,
        "url": sanitized_url(slug, endpoint),
        "response_sha256": sha256_bytes(body),
        "status_code": int(status_code),
        "schema_id": endpoint.schema_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "observed_end_date": verdict.observed_end_date,
        "raw_relpath": raw_rel.as_posix(),
        "normalized_relpath": normalized_rel.as_posix(),
    }
    envelope_rel = Path("envelopes") / slug / f"{endpoint.object_name}.json"
    write_metadata(run_dir / envelope_rel, envelope, config.tokens.values())
    return verdict


def _write_manifest(run_dir: Path, status: str, token_values: Sequence[str]) -> None:
    files: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        files[path.relative_to(run_dir).as_posix()] = sha256_bytes(path.read_bytes())
    normalized = {
        relpath: digest
        for relpath, digest in files.items()
        if relpath.startswith("normalized/")
    }
    manifest = {
        "schema_version": 1,
        "status": status,
        "files": files,
        "normalized_outputs": normalized,
    }
    write_metadata(run_dir / "manifest.json", manifest, token_values)


def run_capture(
    config_path: Path,
    run_dir: Path,
    slugs: Sequence[str],
    grace_days: int = 14,
    run_date: date | None = None,
    *,
    fetch: Fetch | None = None,
) -> int:
    """Run one capture; returns 0 only when every endpoint passes."""
    try:
        config = load_config(Path(config_path))
    except ConfigError as error:
        _report(f"config rejected: {error}")
        return EXIT_USAGE
    if not slugs:
        _report("at least one --slug is required")
        return EXIT_USAGE
    if grace_days < 0:
        _report("freshness grace days must be non-negative")
        return EXIT_USAGE
    try:
        for slug in slugs:
            validate_slug(slug)
    except ValueError as error:
        _report(str(error))
        return EXIT_USAGE
    effective_run_date = run_date or datetime.now(timezone.utc).date()
    effective_fetch = fetch or urllib_fetch

    run_dir = Path(run_dir)
    try:
        create_run_dir(run_dir)
    except RawStoreError as error:
        _report(str(error))
        return EXIT_RUN_FAILED

    coverage: dict[str, Any] = {
        "schema_version": 1,
        "run_date": effective_run_date.isoformat(),
        "freshness_grace_days": int(grace_days),
        "slugs": {},
    }
    failures: list[str] = []
    for slug in slugs:
        per_slug: dict[str, Any] = {}
        coverage["slugs"][slug] = per_slug
        for endpoint in ENDPOINTS:
            try:
                verdict = _capture_endpoint(
                    run_dir=run_dir,
                    slug=slug,
                    endpoint=endpoint,
                    config=config,
                    run_date=effective_run_date,
                    grace_days=grace_days,
                    fetch=effective_fetch,
                )
            except TuringTraderError as error:
                failures.append(f"{slug}/{endpoint.object_name}: {error}")
                per_slug[endpoint.object_name] = {
                    "carries_dates": endpoint.carries_dates,
                    "observed_end_date": None,
                    "verdict": "error",
                    "detail": str(error),
                }
                continue
            if verdict.verdict in FAILING_VERDICTS:
                failures.append(f"{slug}/{endpoint.object_name}: {verdict.detail}")
            per_slug[endpoint.object_name] = {
                "carries_dates": verdict.carries_dates,
                "observed_end_date": verdict.observed_end_date,
                "verdict": verdict.verdict,
                "detail": verdict.detail,
            }

    status = "failed" if failures else "passed"
    coverage["status"] = status
    coverage["failures"] = failures
    token_values = list(config.tokens.values())
    try:
        write_metadata(run_dir / "coverage.json", coverage, token_values)
        _write_manifest(run_dir, status, token_values)
    except RawStoreError as error:
        _report(f"metadata persistence failed: {error}")
        return EXIT_RUN_FAILED
    for failure in failures:
        _report(f"FAILED: {failure}")
    _report(f"capture {status}: {run_dir}")
    return EXIT_OK if not failures else EXIT_RUN_FAILED


def _fail_replay(reason: str) -> int:
    _report(f"replay FAILED: {reason}")
    return EXIT_RUN_FAILED


def run_replay(run_dir: Path) -> int:
    """Verify a run directory byte-for-byte and re-derive its outputs."""
    run_dir = Path(run_dir)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return _fail_replay(f"manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _fail_replay("manifest is not valid JSON")
    recorded: dict[str, str] = manifest.get("files", {})

    on_disk = {
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    extra = on_disk - set(recorded)
    if extra:
        return _fail_replay(f"unexpected files not in manifest: {sorted(extra)}")
    for relpath, digest in sorted(recorded.items()):
        path = run_dir / relpath
        if not path.is_file():
            return _fail_replay(f"manifest file is missing: {relpath}")
        actual = sha256_bytes(path.read_bytes())
        if actual != digest:
            return _fail_replay(f"sha256 mismatch for {relpath}")

    envelope_paths = sorted(run_dir.glob("envelopes/*/*.json"))
    if not envelope_paths:
        return _fail_replay("run directory has no envelopes")
    for envelope_path in envelope_paths:
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        object_name = envelope["object"]
        try:
            endpoint = get_endpoint(object_name)
        except KeyError:
            return _fail_replay(f"envelope names an unknown object: {object_name}")
        try:
            raw_bytes = (run_dir / envelope["raw_relpath"]).read_bytes()
            if sha256_bytes(raw_bytes) != envelope["response_sha256"]:
                return _fail_replay(
                    "raw bytes do not match the envelope hash: "
                    f"{envelope['raw_relpath']}"
                )
            payload = _parse_payload(endpoint, raw_bytes)
            validate(endpoint, payload)
            recomputed = (
                canonical_json(normalize(object_name, payload)) + "\n"
            ).encode("utf-8")
        except TuringTraderError as error:
            return _fail_replay(f"{object_name}: {error}")
        normalized_path = run_dir / envelope["normalized_relpath"]
        if not normalized_path.is_file():
            return _fail_replay(f"normalized output is missing: {normalized_path}")
        if normalized_path.read_bytes() != recomputed:
            return _fail_replay(
                "normalized output is not reproducible: "
                f"{envelope['normalized_relpath']}"
            )
    _report(f"replay verified: {run_dir}")
    return EXIT_OK


def _capture_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-engine-turingtrader",
        description="Governed TuringTrader public-token endpoint capture",
    )
    parser.add_argument("--config", required=True, help="deployment-local token JSON")
    parser.add_argument("--run-dir", required=True, help="create-only run directory")
    parser.add_argument(
        "--slug", action="append", required=True, help="strategy slug (repeatable)"
    )
    parser.add_argument("--freshness-grace-days", type=int, default=14)
    parser.add_argument(
        "--run-date", default=None, help="YYYY-MM-DD (default: today UTC)"
    )
    return parser


def _replay_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quant-engine-turingtrader replay",
        description="Deterministic offline replay of a capture run",
    )
    parser.add_argument("--run-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "replay":
        namespace = _replay_parser().parse_args(args[1:])
        return run_replay(Path(namespace.run_dir))
    namespace = _capture_parser().parse_args(args)
    run_date: date | None = None
    if namespace.run_date is not None:
        try:
            run_date = date.fromisoformat(namespace.run_date)
        except ValueError:
            _report("--run-date must be YYYY-MM-DD")
            return EXIT_USAGE
    return run_capture(
        Path(namespace.config),
        Path(namespace.run_dir),
        list(namespace.slug),
        grace_days=namespace.freshness_grace_days,
        run_date=run_date,
    )


if __name__ == "__main__":
    sys.exit(main())
