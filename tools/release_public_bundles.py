"""Build the public factor and sector bundles of a release cycle from its raw captures (B7).

Replaces the hand scripts ``mk_public_sources.py`` / ``build_public_bundles.py``. Nothing is located
by a retained-archive search and nothing names a worktree: the raw captures, their receipts, the
acquisition run window and the pins come from a *public-sources manifest* (``qe-release-public-sources/1``)
whose paths are relative to ``--raw-root``; the cycle (cutoff, evaluation date, output roots) comes from
the resolved-cycle file (``--resolved-cycle``, ``docs/release-cycle-manifest.md``).

Every raw file is hashed against its pin before any builder runs. The two committed builders
(``portfolio_public_decomposition_sources`` and ``portfolio_public_style_sources``) then write their
create-only bundles under the resolved cycle's ``public_bundles`` roots. Refusals (exit 1, nothing written
by this tool): wrong resolved-cycle schema, a bundle root key the cycle lacks, a feed set that differs from
the builder's, a raw path that escapes ``--raw-root`` or is a symlink, a missing or mismatched file, an
acquisition window that is malformed, reversed, or whose evaluation date is after the cycle's.

Usage::

    release_public_bundles.py --resolved-cycle F --sources-manifest M --raw-root DIR --catalog C
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import portfolio_public_decomposition_sources as factors
import portfolio_public_style_sources as style

RESOLVED_SCHEMA = "qe-release-resolved-cycle/1"
SOURCES_SCHEMA = "qe-release-public-sources/1"
_SHA = re.compile(r"[0-9a-f]{64}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


class Refusal(Exception):
    """A pinned input is missing, changed or inconsistent; nothing was built."""


def _json(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(f"{what} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise Refusal(f"{what} must be a JSON object")
    return value


def _pinned(raw_root: Path, row: object, what: str) -> Path:
    """``raw_root/row.path`` when it is a regular file inside the root whose sha256 equals the pin."""

    if not isinstance(row, Mapping) or set(row) != {"path", "sha256"}:
        raise Refusal(f"{what}: expected exactly {{path, sha256}}")
    relative, pin = row["path"], row["sha256"]
    if not isinstance(pin, str) or not _SHA.fullmatch(pin):
        raise Refusal(f"{what}: sha256 pin is not 64 lowercase hex")
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or "\\" in relative
        or ":" in relative
        or any(part in ("", ".", "..") for part in relative.split("/"))
    ):
        raise Refusal(f"{what}: path is not a safe relative path: {relative!r}")
    current = raw_root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise Refusal(f"{what}: {relative} crosses a symlink")
    if not current.is_file():
        raise Refusal(f"{what}: {relative} is missing under the raw root")
    if hashlib.sha256(current.read_bytes()).hexdigest() != pin:
        raise Refusal(f"{what}: {relative} differs from its pin")
    return current


def _bundle_root(resolved: Mapping[str, Any], key: str) -> Path:
    roots = resolved.get("public_bundles")
    if not isinstance(roots, Mapping) or key not in roots:
        raise Refusal(f"the resolved cycle has no public_bundles.{key}")
    return Path(str(roots[key]))


def build(
    resolved_cycle: Path,
    sources_manifest: Path,
    raw_root: Path,
    catalog: Path,
    *,
    factors_bundle_key: str,
) -> dict[str, Any]:
    resolved = _json(resolved_cycle, "resolved cycle")
    if resolved.get("schema_version") != RESOLVED_SCHEMA:
        raise Refusal(f"resolved cycle schema_version is not {RESOLVED_SCHEMA}")
    spec = _json(sources_manifest, "public-sources manifest")
    if set(spec) != {"schema_version", "acquisition_run", "factor_feeds", "styles"}:
        raise Refusal("public-sources manifest keys differ from the schema")
    if spec["schema_version"] != SOURCES_SCHEMA:
        raise Refusal(f"public-sources manifest schema_version is not {SOURCES_SCHEMA}")
    if not raw_root.is_dir():
        raise Refusal(f"raw root is not a directory: {raw_root}")
    out_factors = _bundle_root(resolved, factors_bundle_key)

    run = spec["acquisition_run"]
    if not isinstance(run, Mapping) or set(run) != {
        "run_started_at_utc",
        "run_ended_at_utc",
        "evaluation_date",
    }:
        raise Refusal(
            "acquisition_run needs run_started_at_utc, run_ended_at_utc, evaluation_date"
        )
    started, ended = str(run["run_started_at_utc"]), str(run["run_ended_at_utc"])
    evaluation = str(run["evaluation_date"])
    if not (
        _STAMP.fullmatch(started)
        and _STAMP.fullmatch(ended)
        and _DATE.fullmatch(evaluation)
    ):
        raise Refusal("acquisition_run timestamps or evaluation_date are malformed")
    if started > ended:
        raise Refusal("acquisition run ends before it starts")
    if evaluation > str(resolved["evaluated_on"]):
        raise Refusal(
            f"evaluation_date {evaluation} is after the cycle's evaluated_on {resolved['evaluated_on']}"
        )

    feeds = spec["factor_feeds"]
    if not isinstance(feeds, Mapping) or set(feeds) != set(factors.SOURCE_KEYS):
        raise Refusal(f"factor_feeds must be exactly {sorted(factors.SOURCE_KEYS)}")
    source_paths: dict[str, Path] = {}
    manifest_paths: dict[str, Path] = {}
    for key in factors.SOURCE_KEYS:
        row = feeds[key]
        if not isinstance(row, Mapping) or set(row) != {"source", "manifest"}:
            raise Refusal(f"factor_feeds.{key} needs source and manifest")
        source_paths[key] = _pinned(
            raw_root, row["source"], f"factor_feeds.{key}.source"
        )
        manifest_paths[key] = _pinned(
            raw_root, row["manifest"], f"factor_feeds.{key}.manifest"
        )
    rows = spec["styles"]
    if not isinstance(rows, list) or not rows:
        raise Refusal("styles must be a non-empty list")
    keys = [str(r.get("bundle_key")) if isinstance(r, Mapping) else "" for r in rows]
    if len(set(keys)) != len(keys):
        raise Refusal("styles repeat a bundle_key")
    builds = []
    for index, row in enumerate(rows):
        what = f"styles[{index}]"
        allowed = {"bundle_key", "source", "manifest", "universe_ids"}
        if (
            not isinstance(row, Mapping)
            or not {"bundle_key", "source", "manifest"} <= set(row) <= allowed
        ):
            raise Refusal(
                f"{what} needs bundle_key, source, manifest and optional universe_ids"
            )
        universes = row.get("universe_ids")
        if universes is not None and (
            not isinstance(universes, list)
            or not universes
            or not all(isinstance(u, str) for u in universes)
        ):
            raise Refusal(
                f"{what}.universe_ids must be a non-empty list of strings when given"
            )
        builds.append(
            (
                _bundle_root(resolved, str(row["bundle_key"])),
                _pinned(raw_root, row["source"], f"{what}.source"),
                _pinned(raw_root, row["manifest"], f"{what}.manifest"),
                universes,
            )
        )

    # Stage everything first: build every bundle in memory (the builders' own validation of
    # catalog, source contents and universes) and refuse an existing destination, so a rejected
    # style input cannot leave the factor bundle behind. Only then does anything write.
    _, factor_manifest = factors.build_bundle(
        catalog, source_paths, manifest_paths, started, ended, evaluation
    )
    planned = [(out_factors, str(factor_manifest["bundle_id"]))]
    for out, source, receipt, universes in builds:
        _, style_manifest = style.build_bundle(catalog, source, receipt, universes)
        planned.append((out, str(style_manifest["bundle_id"])))
    for root, bundle_id in planned:
        if (root.resolve() / bundle_id).exists():
            raise Refusal(f"bundle already exists: {root / bundle_id}")
    factor_result = factors.write_bundle(
        catalog, source_paths, manifest_paths, out_factors, started, ended, evaluation
    )
    style_results = [
        style.write_bundle(catalog, source, receipt, out, universes)
        for out, source, receipt, universes in builds
    ]
    return {
        "cutoff": resolved["cutoff"],
        "factors": {k: factor_result[k] for k in ("bundle_id", "destination")},
        "styles": [
            {k: r[k] for k in ("bundle_id", "destination")} for r in style_results
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved-cycle", type=Path, required=True)
    parser.add_argument("--sources-manifest", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--factors-bundle-key", default="public_factors")
    args = parser.parse_args(argv)
    try:
        result = build(
            args.resolved_cycle,
            args.sources_manifest,
            args.raw_root,
            args.catalog,
            factors_bundle_key=args.factors_bundle_key,
        )
    except (Refusal, OSError, ValueError) as exc:
        print(f"release-public-bundles: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
