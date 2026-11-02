"""Build the baseline spec of a release cycle (RELEASE-DRIVER-001, hand-script block B3).

Replaces the hand scripts ``mk_baseline_spec.py`` and ``patch_baseline_spec.py``: the R2 spec was
the previous release's spec, remapped path by path, with the provenance and provider-ledger blocks
and the cost sources rebound. Here every input is named and verified, nothing is remapped.

Inputs (all verified; a missing or changed one is a refusal, never a fallback):

* ``--resolved-cycle``   the B0 resolved-cycle file (``qe-release-resolved-cycle/1``): cutoff,
  ``cost_inputs`` (files with sha256 and size) and ``providers.*.generation_id``.
* declared inputs        the governed, cutoff-dependent part of the spec (static sources and the
  replay expectation) read through the release-inputs loader
  (``quant_engine.core.release_inputs``): ``--release-inputs-root`` and the manifest pin.
* ``--root ID=DIR``      one directory per root id the declared sources name.
* ``--provenance-generation`` / ``--ledger-generation``  the cycle's two generation directories.
* ``--repo-root``        the checkout whose financial-MVP contract, registry and schema are bound.

``declare`` writes the declared-inputs file from a previous baseline spec (how the first one is
authored); ``build`` writes the spec, create-only, in the canonical byte form of the hand scripts
(sorted keys, compact separators, no trailing newline).
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from quant_engine.core import ReleaseInputsError, load_release_inputs

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = REPO_ROOT / "schemas" / "portfolio"
SPEC_SCHEMA = SCHEMAS / "baseline-spec.schema.json"
DECLARED_SCHEMA = SCHEMAS / "baseline-declared-inputs.schema.json"
RESOLVED_VERSION = "qe-release-resolved-cycle/1"
DECLARED_VERSION = "qe-baseline-declared-inputs/1"
DECLARED_PATH = "ops/mvp2-release/inputs/baseline/baseline-declared-inputs.json"
LEDGER_ROOT_ID = "provider-ledger"
ECONOMICS_NAME = "economics"
EVIDENCE_NAME = "evidence"
# The financial-MVP binding names three repository files; these are repo-relative constants.
FINANCIAL_MVP = (
    ("contract", "contracts/portfolio-financial-mvp-source-v1.json"),
    ("registry", "docs/analytics/current-strategy-decomposition-plan.json"),
    ("schema", "schemas/portfolio/financial-mvp-source-contract.schema.json"),
)
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ROOT_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
# Static sources a build may not take from the declared inputs: the cost files come from the
# cycle, so a declared copy would silently shadow them.
_CYCLE_SOURCES = ("cost-economics", "cost-evidence")


class BaselineSpecError(Exception):
    """A refusal; ``str(error)`` is ``CODE: detail``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise BaselineSpecError(
            "INPUT", f"{what} is not readable JSON: {path}"
        ) from exc


def _schema_check(value: Any, schema_path: Path, code: str, what: str) -> None:
    import jsonschema  # type: ignore[import-untyped]

    validator = jsonschema.Draft202012Validator(
        json.loads(schema_path.read_bytes()), format_checker=jsonschema.FormatChecker()
    )
    errors = sorted(
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(value)
    )
    if errors:
        raise BaselineSpecError(code, f"{what}: " + "; ".join(errors[:5]))


def _date(value: str, what: str) -> str:
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise BaselineSpecError(
            "INPUT", f"{what} is not an ISO date: {value!r}"
        ) from exc


def _existing_dir(value: str, what: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise BaselineSpecError(
            "ROOT_MISSING", f"{what} is not an absolute directory: {value}"
        )
    return path


def _pinned_file(path: Path, sha256: str, what: str, size: int | None = None) -> bytes:
    if not path.is_file():
        raise BaselineSpecError("MISSING", f"{what} does not exist: {path}")
    data = path.read_bytes()
    if _sha256(data) != sha256:
        raise BaselineSpecError("HASH_MISMATCH", f"{what} differs from its pin: {path}")
    if size is not None and len(data) != size:
        raise BaselineSpecError(
            "SIZE_MISMATCH", f"{what} size differs from its pin: {path}"
        )
    return data


def _resolved_cycle(path: Path) -> dict[str, Any]:
    resolved = _read_json(path, "resolved cycle")
    if (
        not isinstance(resolved, dict)
        or resolved.get("schema_version") != RESOLVED_VERSION
    ):
        raise BaselineSpecError(
            "RESOLVED_CYCLE", f"not a {RESOLVED_VERSION} file: {path}"
        )
    _schema_check(
        resolved,
        SCHEMAS / "release-resolved-cycle.schema.json",
        "RESOLVED_CYCLE",
        "resolved cycle",
    )
    _date(resolved["cutoff"], "resolved cycle cutoff")
    return resolved


def _generation(directory: Path, what: str) -> tuple[Path, str, str]:
    """``(root, generation_id, sha256 of generation.json)`` of ``<root>/generations/<id>``."""

    if directory.parent.name != "generations":
        raise BaselineSpecError(
            "GENERATION", f"{what} is not under a generations directory"
        )
    gid = directory.name
    manifest = directory / "generation.json"
    if not manifest.is_file():
        raise BaselineSpecError(
            "MISSING", f"{what} has no generation.json: {directory}"
        )
    named = _read_json(manifest, f"{what} generation.json").get("generation_id")
    if named != gid:
        raise BaselineSpecError(
            "ID_MISMATCH", f"{what} directory {gid} but manifest names {named}"
        )
    return directory.parent.parent, gid, _sha256(manifest.read_bytes())


def _cost_sources(
    resolved: Mapping[str, Any], declared: Mapping[str, Any], cost_root_id: str
) -> tuple[Path, dict[str, dict[str, Any]]]:
    costs = resolved["cost_inputs"]
    if ECONOMICS_NAME not in costs:
        raise BaselineSpecError(
            "COST_INPUT", f"resolved cycle has no cost input {ECONOMICS_NAME!r}"
        )
    unknown = sorted(set(costs) - {ECONOMICS_NAME, EVIDENCE_NAME})
    if unknown:
        raise BaselineSpecError(
            "COST_INPUT", f"cost inputs the baseline does not use: {unknown}"
        )
    files = {name: Path(ref["path"]) for name, ref in costs.items()}
    parents = {path.parent for path in files.values()}
    if len(parents) != 1:
        raise BaselineSpecError("COST_INPUT", "cost inputs must sit in one directory")
    (directory,) = parents
    for name, ref in costs.items():
        _pinned_file(
            files[name], ref["sha256"], f"cost input {name}", ref["size_bytes"]
        )
    meta = declared["cost_economics"]
    sources = {
        "cost-economics": {
            "as_of_date": meta["as_of_date"],
            "expected_sha256": costs[ECONOMICS_NAME]["sha256"],
            "relative_path": files[ECONOMICS_NAME].name,
            "root_id": cost_root_id,
            "schema_name": meta["schema_name"],
            "schema_version": meta["schema_version"],
        }
    }
    if EVIDENCE_NAME in costs:
        evidence = _read_json(files[EVIDENCE_NAME], "cost evidence")
        name, _, version = str(evidence.get("schema", "")).partition("/")
        feeds = [
            item["feed_last_date"]
            for row in evidence.get("rows", [])
            for item in row.get("corroboration", [])
            if isinstance(item, dict) and "feed_last_date" in item
        ]
        if not name or not version or not feeds:
            raise BaselineSpecError(
                "COST_INPUT", "cost evidence has no schema or feed dates"
            )
        sources["cost-evidence"] = {
            "as_of_date": max(
                _date(feed, "cost evidence feed_last_date") for feed in feeds
            ),
            "expected_sha256": costs[EVIDENCE_NAME]["sha256"],
            "relative_path": files[EVIDENCE_NAME].name,
            "root_id": cost_root_id,
            "schema_name": name,
            "schema_version": version,
        }
    return directory, sources


def _ledger_cross_check(
    ledger_dir: Path, resolved: Mapping[str, Any], cutoff: str
) -> str:
    catalog_path = ledger_dir / "provider-ledger.json"
    if not catalog_path.is_file():
        raise BaselineSpecError(
            "MISSING", f"ledger generation has no provider-ledger.json: {ledger_dir}"
        )
    catalog_bytes = catalog_path.read_bytes()
    catalog = json.loads(catalog_bytes)
    if catalog.get("as_of_date") != cutoff:
        raise BaselineSpecError(
            "CUTOFF_MISMATCH",
            f"ledger as_of_date {catalog.get('as_of_date')} is not the cutoff {cutoff}",
        )
    rows = {row["provider_id"]: row for row in catalog.get("provider_generations", [])}
    wanted = {
        pid: (row["generation_id"], row["manifest_sha256"])
        for pid, row in resolved["providers"].items()
    }
    have = {
        pid: (row["generation_id"], row["manifest_sha256"]) for pid, row in rows.items()
    }
    if wanted != have:
        bad = sorted(
            pid for pid in set(wanted) | set(have) if wanted.get(pid) != have.get(pid)
        )
        raise BaselineSpecError(
            "LEDGER_PROVIDERS",
            f"ledger admitted a different provider generation than the cycle for: {bad}",
        )
    return _sha256(catalog_bytes)


def build(
    *,
    resolved_cycle: Path,
    release_inputs_root: Path,
    release_inputs_manifest_sha256: str,
    declared_path: str,
    roots: Mapping[str, str],
    cost_root_id: str,
    provenance_generation: Path,
    ledger_generation: Path,
    repo_root: Path,
) -> dict[str, Any]:
    resolved = _resolved_cycle(resolved_cycle)
    cutoff = resolved["cutoff"]
    try:
        inputs = load_release_inputs(
            release_inputs_root, release_inputs_manifest_sha256
        )
        declared_file = inputs.path(declared_path)
    except ReleaseInputsError as exc:
        raise BaselineSpecError("RELEASE_INPUTS", str(exc)) from exc
    if inputs.cutoff != cutoff:
        raise BaselineSpecError(
            "CUTOFF_MISMATCH",
            f"release inputs are for {inputs.cutoff}, the cycle cutoff is {cutoff}",
        )
    declared = _read_json(declared_file, "declared inputs")
    _schema_check(declared, DECLARED_SCHEMA, "DECLARED_INPUTS", "declared inputs")
    if declared["cutoff"] != cutoff:
        raise BaselineSpecError(
            "CUTOFF_MISMATCH",
            f"declared inputs are for {declared['cutoff']}, the cycle cutoff is {cutoff}",
        )
    sources = {name: dict(ref) for name, ref in declared["sources"].items()}
    if not _ROOT_ID.match(cost_root_id):
        raise BaselineSpecError("ROOTS", f"bad cost root id {cost_root_id!r}")
    needed = {ref["root_id"] for ref in sources.values()}
    if (
        cost_root_id in needed
        or cost_root_id == LEDGER_ROOT_ID
        or LEDGER_ROOT_ID in needed
    ):
        raise BaselineSpecError(
            "ROOTS",
            f"root id {cost_root_id!r} or {LEDGER_ROOT_ID!r} collides with a declared root",
        )
    if set(roots) != needed:
        raise BaselineSpecError(
            "ROOTS", f"--root must name exactly {sorted(needed)}; got {sorted(roots)}"
        )
    root_dirs = {
        rid: _existing_dir(roots[rid], f"root {rid}") for rid in sorted(needed)
    }
    for name, ref in sources.items():
        _pinned_file(
            root_dirs[ref["root_id"]] / ref["relative_path"],
            ref["expected_sha256"],
            f"source {name}",
        )

    cost_dir, cost_sources = _cost_sources(resolved, declared, cost_root_id)
    sources.update(cost_sources)
    prov_root, prov_gid, _ = _generation(provenance_generation, "provenance generation")
    prov_catalog = provenance_generation / "portfolio-provenance.json"
    if not prov_catalog.is_file():
        raise BaselineSpecError(
            "MISSING", "provenance generation has no portfolio-provenance.json"
        )
    ledger_root, ledger_gid, ledger_manifest_sha = _generation(
        ledger_generation, "ledger generation"
    )
    ledger_catalog_sha = _ledger_cross_check(ledger_generation, resolved, cutoff)

    mvp: dict[str, str] = {}
    for key, relative in FINANCIAL_MVP:
        path = repo_root / relative
        if not path.is_file():
            raise BaselineSpecError(
                "MISSING", f"financial MVP {key} not in the repository: {relative}"
            )
        mvp[f"{key}_path"] = str(path)
        mvp[f"{key}_sha256"] = _sha256(path.read_bytes())

    spec = {
        "schema_version": declared["spec_schema_version"],
        "policy_version": declared["policy_version"],
        "as_of_date": cutoff,
        "provenance": {
            "catalog_sha256": _sha256(prov_catalog.read_bytes()),
            "generation_id": prov_gid,
            "root": str(prov_root),
        },
        "roots": {
            **{rid: str(path) for rid, path in root_dirs.items()},
            cost_root_id: str(cost_dir),
            LEDGER_ROOT_ID: str(ledger_root),
        },
        "sources": sources,
        "provider_ledger": {
            "catalog_sha256": ledger_catalog_sha,
            "financial_mvp": mvp,
            "generation_id": ledger_gid,
            "manifest_sha256": ledger_manifest_sha,
            "root_id": LEDGER_ROOT_ID,
        },
        "replay": declared["replay"],
    }
    _schema_check(spec, SPEC_SCHEMA, "SPEC_SCHEMA", "built baseline spec")
    return spec


def declare(previous: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Declared inputs and the root directories of a previous baseline spec."""

    _schema_check(previous, SPEC_SCHEMA, "SPEC_SCHEMA", "previous baseline spec")
    sources = {k: dict(v) for k, v in previous["sources"].items()}
    economics = sources.get("cost-economics")
    if economics is None:
        raise BaselineSpecError("INPUT", "previous spec has no cost-economics source")
    static = {k: v for k, v in sources.items() if k not in _CYCLE_SOURCES}
    used = {ref["root_id"] for ref in static.values()}
    declared = {
        "schema_version": DECLARED_VERSION,
        "cutoff": previous["as_of_date"],
        "spec_schema_version": previous["schema_version"],
        "policy_version": previous["policy_version"],
        "cost_economics": {
            "as_of_date": economics["as_of_date"],
            "schema_name": economics["schema_name"],
            "schema_version": economics["schema_version"],
        },
        "sources": static,
        "replay": previous["replay"],
    }
    _schema_check(declared, DECLARED_SCHEMA, "DECLARED_INPUTS", "declared inputs")
    return declared, {rid: previous["roots"][rid] for rid in sorted(used)}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _create_only(path: Path, data: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise BaselineSpecError(
            "OUTPUT_EXISTS", f"refusing to overwrite {path}"
        ) from exc


def _roots(items: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        rid, sep, directory = item.partition("=")
        if not sep or not _ROOT_ID.match(rid) or not directory:
            raise BaselineSpecError("ROOTS", f"--root must be ID=DIR, got {item!r}")
        if rid in out:
            raise BaselineSpecError("ROOTS", f"root {rid} given twice")
        out[rid] = directory
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    b = commands.add_parser("build")
    b.add_argument("--resolved-cycle", type=Path, required=True)
    b.add_argument("--release-inputs-root", type=Path, required=True)
    b.add_argument("--release-inputs-manifest-sha256", required=True)
    b.add_argument("--declared-inputs-path", default=DECLARED_PATH)
    b.add_argument("--root", action="append", default=[], metavar="ID=DIR")
    b.add_argument("--cost-root-id", default="cost-inputs")
    b.add_argument("--provenance-generation", type=Path, required=True)
    b.add_argument("--ledger-generation", type=Path, required=True)
    b.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    b.add_argument("--out", type=Path, required=True)
    d = commands.add_parser("declare")
    d.add_argument("--previous-spec", type=Path, required=True)
    d.add_argument("--out", type=Path, required=True)
    d.add_argument("--roots-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            spec = build(
                resolved_cycle=args.resolved_cycle,
                release_inputs_root=args.release_inputs_root,
                release_inputs_manifest_sha256=args.release_inputs_manifest_sha256,
                declared_path=args.declared_inputs_path,
                roots=_roots(args.root),
                cost_root_id=args.cost_root_id,
                provenance_generation=args.provenance_generation,
                ledger_generation=args.ledger_generation,
                repo_root=args.repo_root,
            )
            _create_only(args.out, _canonical(spec))
            print(json.dumps({"baseline_spec_sha256": _sha256(_canonical(spec))}))
        else:
            declared, roots = declare(_read_json(args.previous_spec, "previous spec"))
            _create_only(
                args.out,
                (json.dumps(declared, indent=1, sort_keys=True) + "\n").encode(),
            )
            _create_only(
                args.roots_out,
                (json.dumps(roots, indent=1, sort_keys=True) + "\n").encode(),
            )
    except BaselineSpecError as exc:
        print(f"release-baseline-spec: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
