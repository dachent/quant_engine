"""Assemble the admitted Quantbase source files of a release cycle into one directory (B7).

Replaces the hand script ``mk_qb_sources.py``. The six files (accounts, account activity, and the
dashboard price histories) are pinned by sha256 in the sleeve policy manifest
(``quantbase-fund-facts.sources``). Candidates are the files under each ``--source-dir`` and the
members of each ``--source-zip``; a file is taken only when its bytes equal the pin, so no archive
name, release date or worktree is named here. The policy manifest is read either from an explicit
``--policy-manifest`` or from a pinned release-inputs root (``core.release_inputs``, whose cutoff must
equal the resolved cycle's).

The result is written create-only into ``--output-dir`` (which must not exist), through a sibling
temporary directory, so a refusal leaves nothing behind. A receipt ``<output-dir>.receipt.json`` beside it
records the cycle id, the Quantbase generation, the policy manifest sha256 and each file's sha256 and
source; it is outside the directory because ``build_sleeve_facts_release.py`` reads the directory.

Refusals (exit 1): resolved cycle of the wrong schema or without ``providers.quantbase``; policy manifest of
another cutoff or without the six pins; a pinned file missing from every candidate; a candidate with the pinned
name but other bytes and no candidate with the right ones; two pins naming one file; an existing output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any, Sequence

from quant_engine.core import ReleaseInputsError, load_release_inputs

RESOLVED_SCHEMA = "qe-release-resolved-cycle/1"
POLICY_LOGICAL = "ops/mvp2-release/inputs/sleeves/sleeve-policy-manifest.json"


class Refusal(Exception):
    """The Quantbase sources cannot be assembled; nothing was written."""


def _json(raw: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(f"{what} is not JSON") from exc
    if not isinstance(value, dict):
        raise Refusal(f"{what} must be a JSON object")
    return value


def pins(policy: dict[str, Any]) -> dict[str, str]:
    """File name -> sha256 for the accounts, activity and dashboard files of the policy manifest."""

    try:
        src = policy["quantbase-fund-facts"]["sources"]
        rows = [src["account_activity"], src["accounts"], *src["dashboards"].values()]
        found = {str(row["file"]): str(row["sha256"]) for row in rows}
    except (KeyError, TypeError, AttributeError) as exc:
        raise Refusal(
            "policy manifest lacks quantbase-fund-facts.sources pins"
        ) from exc
    if len(found) != len(rows):
        raise Refusal("policy manifest pins one file name twice")
    for name, sha in found.items():
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise Refusal(f"policy pin names a path, not a file: {name!r}")
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise Refusal(f"policy pin for {name} is not a sha256")
    return found


def _candidates(
    dirs: Sequence[Path], zips: Sequence[Path], names: set[str]
) -> dict[str, list[tuple[str, bytes]]]:
    """Base name -> [(origin, bytes)] for the candidate files with one of the wanted names."""

    found: dict[str, list[tuple[str, bytes]]] = {}
    for directory in dirs:
        if not directory.is_dir():
            raise Refusal(f"source directory is not a directory: {directory}")
        for item in sorted(directory.rglob("*")):
            if item.name in names and item.is_file() and not item.is_symlink():
                found.setdefault(item.name, []).append(
                    (f"dir:{item.relative_to(directory).as_posix()}", item.read_bytes())
                )
    for archive in zips:
        try:
            handle = zipfile.ZipFile(archive)
        except (OSError, zipfile.BadZipFile) as exc:
            raise Refusal(f"source zip is unreadable: {archive}") from exc
        with handle:
            for info in handle.infolist():
                name = info.filename.rsplit("/", 1)[-1]
                if name in names and not info.is_dir():
                    found.setdefault(name, []).append(
                        (f"zip:{info.filename}", handle.read(info))
                    )
    return found


def assemble(
    resolved_cycle: Path,
    policy_bytes: bytes,
    policy_sha256: str,
    dirs: Sequence[Path],
    zips: Sequence[Path],
    output_dir: Path,
) -> dict[str, Any]:
    try:
        resolved = json.loads(resolved_cycle.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal(f"resolved cycle is unreadable: {resolved_cycle}") from exc
    if (
        not isinstance(resolved, dict)
        or resolved.get("schema_version") != RESOLVED_SCHEMA
    ):
        raise Refusal(f"resolved cycle schema_version is not {RESOLVED_SCHEMA}")
    quantbase = resolved.get("providers", {}).get("quantbase")
    if not isinstance(quantbase, dict) or "generation_id" not in quantbase:
        raise Refusal("the resolved cycle has no providers.quantbase generation")
    wanted = pins(_json(policy_bytes, "policy manifest"))
    if output_dir.exists():
        raise Refusal(f"output directory already exists: {output_dir}")
    pool = _candidates(dirs, zips, set(wanted))
    chosen: dict[str, tuple[str, bytes]] = {}
    problems: list[str] = []
    for name, sha in sorted(wanted.items()):
        options = pool.get(name, [])
        match = [item for item in options if hashlib.sha256(item[1]).hexdigest() == sha]
        if match:
            chosen[name] = match[0]
        elif options:
            problems.append(
                f"{name}: {len(options)} candidate(s) with that name, none equals the pin {sha[:12]}"
            )
        else:
            problems.append(f"{name}: no candidate (pin {sha[:12]})")
    if problems:
        raise Refusal("; ".join(problems))
    staging = output_dir.with_name(output_dir.name + ".partial")
    if staging.exists():
        raise Refusal(f"a partial output exists from an earlier attempt: {staging}")
    staging.mkdir(parents=True)
    try:
        for name, (_, data) in chosen.items():
            (staging / name).write_bytes(data)
        staging.rename(output_dir)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    receipt = {
        "kind": "qe-release-quantbase-sources-receipt",
        "schema_version": 1,
        "cycle_id": resolved.get("cycle_id"),
        "cutoff": resolved.get("cutoff"),
        "quantbase_generation_id": quantbase["generation_id"],
        "policy_manifest_sha256": policy_sha256,
        "files": {
            name: {"sha256": wanted[name], "source": origin}
            for name, (origin, _) in sorted(chosen.items())
        },
    }
    receipt_path = output_dir.with_name(output_dir.name + ".receipt.json")
    receipt_path.write_bytes(
        (json.dumps(receipt, indent=1, sort_keys=True) + "\n").encode("utf-8")
    )
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved-cycle", type=Path, required=True)
    parser.add_argument("--policy-manifest", type=Path)
    parser.add_argument("--release-inputs-root", type=Path)
    parser.add_argument("--release-inputs-manifest-sha256")
    parser.add_argument("--source-dir", type=Path, action="append", default=[])
    parser.add_argument("--source-zip", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if (args.policy_manifest is None) == (args.release_inputs_root is None):
            raise Refusal(
                "give exactly one of --policy-manifest and --release-inputs-root"
            )
        if args.policy_manifest is not None:
            policy_file = args.policy_manifest
        else:
            if not args.release_inputs_manifest_sha256:
                raise Refusal(
                    "--release-inputs-root needs --release-inputs-manifest-sha256"
                )
            try:
                inputs = load_release_inputs(
                    args.release_inputs_root, args.release_inputs_manifest_sha256
                )
                policy_file = inputs.path(POLICY_LOGICAL)
            except ReleaseInputsError as exc:
                raise Refusal(f"release inputs: {exc}") from exc
            cycle = json.loads(args.resolved_cycle.read_bytes().decode("utf-8"))
            if inputs.cutoff != cycle.get("cutoff"):
                raise Refusal(
                    f"release inputs are for cutoff {inputs.cutoff}, the cycle's is {cycle.get('cutoff')}"
                )
        raw = policy_file.read_bytes()
        receipt = assemble(
            args.resolved_cycle,
            raw,
            hashlib.sha256(raw).hexdigest(),
            args.source_dir,
            args.source_zip,
            args.output_dir,
        )
    except (Refusal, OSError, ValueError) as exc:
        print(f"release-quantbase-sources: FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps({"files": len(receipt["files"]), "output_dir": str(args.output_dir)})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
